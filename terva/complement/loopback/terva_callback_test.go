//go:build complemau

package complemau_tests

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"testing"
	"time"

	"github.com/matrix-org/complement/b"
)

// Check the forwarded callback path before the test sends its one-shot ping.
// GET on this non-ping path returns 404 without invoking either ping handler.
func tervaWaitForPingCallback(t *testing.T, port uint16) {
	t.Helper()
	registration, err := url.Parse(b.ComplemauASURL)
	if err != nil || registration.Hostname() == "" {
		t.Fatal("terva callback: invalid registration host")
	}
	// A test process on the host itself, as terva/complement/rig runs it, cannot
	// resolve the registration host on Linux. There the check can only reach
	// the listener, and TERVA_CALLBACK_READINESS_HOST names where to find it.
	host := registration.Hostname()
	if override := os.Getenv("TERVA_CALLBACK_READINESS_HOST"); override != "" {
		host = override
	}
	target := url.URL{Scheme: "http", Host: net.JoinHostPort(host, strconv.Itoa(int(port))), Path: "/_terva_callback_readiness"}
	transport := &http.Transport{DisableKeepAlives: true}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: 250 * time.Millisecond,
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	started := time.Now()
	attempts, err := tervaAwaitCallback(ctx, client, target.String(), 20*time.Millisecond)
	if err != nil {
		t.Fatalf("terva callback: readiness failed on port %d after %d attempts: %v", port, attempts, err)
	}
	t.Logf("terva callback ready port=%d attempts=%d elapsed=%s", port, attempts, time.Since(started))
}

func tervaAwaitCallback(ctx context.Context, client *http.Client, target string, interval time.Duration) (int, error) {
	for attempts := 1; ; attempts++ {
		request, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
		if err != nil {
			return attempts, err
		}
		response, err := client.Do(request)
		if err == nil {
			response.Body.Close()
			if response.StatusCode == http.StatusNotFound {
				return attempts, nil
			}
		}
		timer := time.NewTimer(interval)
		select {
		case <-ctx.Done():
			timer.Stop()
			return attempts, fmt.Errorf("callback did not return HTTP 404: %w", ctx.Err())
		case <-timer.C:
		}
	}
}
