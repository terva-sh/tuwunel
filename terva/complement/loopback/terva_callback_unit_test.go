//go:build complemau && terva_callback_unit

package complemau_tests

import (
	"context"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"
)

type tervaCallbackTransport func(*http.Request) (*http.Response, error)

func (f tervaCallbackTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestTervaCallbackReadiness(t *testing.T) {
	t.Run("retry connection refusal and wrong status", func(t *testing.T) {
		calls := 0
		client := &http.Client{Transport: tervaCallbackTransport(func(r *http.Request) (*http.Response, error) {
			calls++
			if r.Method != "GET" || r.URL.Path != "/_terva_callback_readiness" {
				t.Fatal("probe invoked a ping")
			}
			if calls == 1 {
				return nil, errors.New("connection refused")
			}
			status := http.StatusServiceUnavailable
			if calls == 3 {
				status = http.StatusNotFound
			}
			return &http.Response{StatusCode: status, Body: io.NopCloser(strings.NewReader("")), Header: make(http.Header)}, nil
		})}
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		attempts, err := tervaAwaitCallback(ctx, client, "http://callback.invalid/_terva_callback_readiness", time.Millisecond)
		if err != nil || attempts != 3 {
			t.Fatalf("attempts=%d error=%v", attempts, err)
		}
	})
	t.Run("fail closed at deadline", func(t *testing.T) {
		client := &http.Client{Transport: tervaCallbackTransport(func(*http.Request) (*http.Response, error) {
			return nil, errors.New("connection refused")
		})}
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Millisecond)
		defer cancel()
		_, err := tervaAwaitCallback(ctx, client, "http://callback.invalid/_terva_callback_readiness", time.Millisecond)
		if !errors.Is(err, context.DeadlineExceeded) {
			t.Fatalf("expected deadline, got %v", err)
		}
	})
}
