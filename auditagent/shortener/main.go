// AuditAgent v2.5 — URL Shortener
//
// A small, dependency-free HTTP service for shortening AuditAgent report
// links so they're easy to share (Slack, PR descriptions, client emails).
// This is the "deliverable moment" of the pipeline per the project plan —
// every audit run should end in a link someone can actually click.
//
// Deliberately standard-library only, no external Go modules: this keeps
// the service to a single static binary with zero dependency-fetching at
// build time, and matches why Go was chosen for this piece in the first
// place (a narrow, high-throughput, low-latency redirect service).
//
// Storage: an in-memory map guarded by a RWMutex, persisted to a JSON file
// on every write so links survive a restart. This is intentionally simple
// for a personal-project scale — the project plan's suggested upgrade path
// (Postgres for the mapping table, Redis in front for hot-link lookups) is
// documented in README.md as the next step once real traffic shows up;
// swapping the Store interface's backing implementation is the only change
// that upgrade would require, the HTTP layer doesn't need to change.
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"os"
	"sync"
	"time"
)

// ---- Storage --------------------------------------------------------------

type Entry struct {
	LongURL   string    `json:"long_url"`
	CreatedAt time.Time `json:"created_at"`
	Clicks    int       `json:"clicks"`
}

type Store struct {
	mu      sync.RWMutex
	entries map[string]*Entry
	counter uint64
	path    string
}

type persistedState struct {
	Entries map[string]*Entry `json:"entries"`
	Counter uint64            `json:"counter"`
}

func NewStore(path string) (*Store, error) {
	s := &Store{
		entries: make(map[string]*Entry),
		path:    path,
	}
	if err := s.load(); err != nil && !errors.Is(err, os.ErrNotExist) {
		return nil, fmt.Errorf("loading store: %w", err)
	}
	return s, nil
}

func (s *Store) load() error {
	data, err := os.ReadFile(s.path)
	if err != nil {
		return err
	}
	var state persistedState
	if err := json.Unmarshal(data, &state); err != nil {
		return fmt.Errorf("parsing store file: %w", err)
	}
	s.entries = state.Entries
	if s.entries == nil {
		s.entries = make(map[string]*Entry)
	}
	s.counter = state.Counter
	return nil
}

// save must be called with s.mu already held (read or write lock) by the
// caller's higher-level operation — it just serializes current state.
func (s *Store) save() error {
	state := persistedState{Entries: s.entries, Counter: s.counter}
	data, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return err
	}
	tmp := s.path + ".tmp"
	if err := os.WriteFile(tmp, data, 0644); err != nil {
		return err
	}
	return os.Rename(tmp, s.path) // atomic on POSIX — avoids a torn write on crash
}

// base62 alphabet — avoids ambiguous-looking characters in short URLs
// (no 0/O or 1/l/I confusion) since these are meant to be typed/read by
// humans, not just clicked.
const base62Alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

func toBase62(n uint64) string {
	if n == 0 {
		return string(base62Alphabet[0])
	}
	base := uint64(len(base62Alphabet))
	var buf []byte
	for n > 0 {
		buf = append([]byte{base62Alphabet[n%base]}, buf...)
		n /= base
	}
	return string(buf)
}

// Shorten creates a new short code for longURL, or returns the existing
// code if this exact URL was already shortened (avoids link sprawl from
// the same report being shortened twice).
func (s *Store) Shorten(longURL string) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	for code, entry := range s.entries {
		if entry.LongURL == longURL {
			return code, nil
		}
	}

	s.counter++
	code := toBase62(s.counter)
	s.entries[code] = &Entry{LongURL: longURL, CreatedAt: time.Now().UTC()}

	if err := s.save(); err != nil {
		return "", fmt.Errorf("persisting new short link: %w", err)
	}
	return code, nil
}

// Resolve looks up a code, incrementing its click count as a side effect.
// Returns (entry, found).
func (s *Store) Resolve(code string) (*Entry, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()

	entry, ok := s.entries[code]
	if !ok {
		return nil, false
	}
	entry.Clicks++
	if err := s.save(); err != nil {
		// Don't fail the redirect over a persistence hiccup — log and
		// continue serving the user. Losing one click-count increment is
		// a much smaller problem than a broken redirect.
		log.Printf("warning: failed to persist click count for %s: %v", code, err)
	}
	return entry, true
}

// Stats is a read-only lookup that does NOT increment clicks — used by the
// /stats endpoint so checking analytics doesn't itself inflate them.
func (s *Store) Stats(code string) (*Entry, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	entry, ok := s.entries[code]
	return entry, ok
}

// ---- HTTP handlers ----------------------------------------------------------

type Server struct {
	store   *Store
	baseURL string
}

type shortenRequest struct {
	URL string `json:"url"`
}

type shortenResponse struct {
	ShortCode string `json:"short_code"`
	ShortURL  string `json:"short_url"`
	LongURL   string `json:"long_url"`
}

func (srv *Server) handleShorten(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	var req shortenRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, `{"error":"invalid JSON body, expected {\"url\": \"...\"}"}`, http.StatusBadRequest)
		return
	}

	parsed, err := url.ParseRequestURI(req.URL)
	if err != nil || (parsed.Scheme != "http" && parsed.Scheme != "https") {
		http.Error(w, `{"error":"url must be a valid absolute http(s) URL"}`, http.StatusBadRequest)
		return
	}

	code, err := srv.store.Shorten(req.URL)
	if err != nil {
		log.Printf("error shortening URL: %v", err)
		http.Error(w, `{"error":"internal error"}`, http.StatusInternalServerError)
		return
	}

	resp := shortenResponse{
		ShortCode: code,
		ShortURL:  fmt.Sprintf("%s/%s", srv.baseURL, code),
		LongURL:   req.URL,
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

func (srv *Server) handleRedirect(w http.ResponseWriter, r *http.Request) {
	code := r.URL.Path[1:] // strip leading "/"
	if code == "" {
		http.NotFound(w, r)
		return
	}

	entry, ok := srv.store.Resolve(code)
	if !ok {
		http.Error(w, `{"error":"short link not found"}`, http.StatusNotFound)
		return
	}
	http.Redirect(w, r, entry.LongURL, http.StatusFound)
}

func (srv *Server) handleStats(w http.ResponseWriter, r *http.Request) {
	code := r.URL.Path[len("/stats/"):]
	if code == "" {
		http.NotFound(w, r)
		return
	}
	entry, ok := srv.store.Stats(code)
	if !ok {
		http.Error(w, `{"error":"short link not found"}`, http.StatusNotFound)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(entry)
}

func handleHealth(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
	w.Write([]byte(`{"status":"ok"}`))
}

func main() {
	port := flag.String("port", "8080", "port to listen on")
	baseURL := flag.String("base-url", "", "base URL used when constructing short links (e.g. http://localhost:8080). Defaults to http://localhost:<port>")
	storePath := flag.String("store", "shortener_data.json", "path to the JSON file used for persistence")
	flag.Parse()

	if *baseURL == "" {
		*baseURL = "http://localhost:" + *port
	}

	store, err := NewStore(*storePath)
	if err != nil {
		log.Fatalf("failed to initialize store: %v", err)
	}

	srv := &Server{store: store, baseURL: *baseURL}

	mux := http.NewServeMux()
	mux.HandleFunc("/shorten", srv.handleShorten)
	mux.HandleFunc("/health", handleHealth)
	mux.HandleFunc("/stats/", srv.handleStats)
	mux.HandleFunc("/", srv.handleRedirect) // catch-all: GET /{code}

	addr := ":" + *port
	log.Printf("AuditAgent shortener listening on %s (base URL: %s, store: %s)", addr, *baseURL, *storePath)
	if err := http.ListenAndServe(addr, mux); err != nil {
		log.Fatalf("server error: %v", err)
	}
}
