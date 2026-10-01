package docker

import (
	"context"
	"fmt"
	"os"
	"strconv"
	"testing"
	"time"

	"net/netip"
	"github.com/matrix-org/complement/b"
	"github.com/matrix-org/complement/config"
	"github.com/moby/moby/api/types/network"
	"github.com/moby/moby/client"
)

func tervaPort(s string) network.Port { return network.MustParsePort(s) }

func tervaBindings(expected network.PortMap, host netip.Addr, port string) network.PortMap {
	actual := network.PortMap{}
	for p := range expected {
		actual[p] = []network.PortBinding{{HostIP: host, HostPort: port}}
	}
	return actual
}

func TestTervaLoopbackPolicy(t *testing.T) {
	exposed := network.PortSet{tervaPort("8008/tcp"): {}, tervaPort("8448/tcp"): {}, tervaPort("9000/udp"): {}}
	expected, err := tervaRequestedPorts(exposed)
	if err != nil || len(expected) != 3 {
		t.Fatal(expected, err)
	}
	if err := tervaValidatePorts(expected, expected, false); err != nil {
		t.Fatal(err)
	}
	// Podman allocates the host port at create, before start.
	created := tervaBindings(expected, tervaLoopbackIP, "30000")
	if err := tervaValidatePorts(created, expected, false); err != nil {
		t.Fatal("podman create-time port refused:", err)
	}
	for _, host := range []netip.Addr{{}, netip.MustParseAddr("0.0.0.0"), netip.MustParseAddr("::"), netip.MustParseAddr("::1"),
		netip.MustParseAddr("192.0.2.1"), netip.MustParseAddr("127.0.0.2"), netip.MustParseAddr("::ffff:127.0.0.1")} {
		t.Run("reject_"+host.String(), func(t *testing.T) {
			actual := tervaBindings(expected, host, "30000")
			for _, published := range []bool{true, false} {
				if tervaValidatePorts(actual, expected, published) == nil {
					t.Fatal("unsafe binding accepted, published:", published)
				}
			}
		})
	}
	for _, port := range []string{"0", "-1", "65536", "bad"} {
		actual := tervaBindings(expected, tervaLoopbackIP, port)
		if tervaValidatePorts(actual, expected, false) == nil {
			t.Fatal("invalid requested port accepted", port)
		}
	}
	for _, port := range []string{"", "0", "-1", "65536", "bad"} {
		actual := tervaBindings(expected, tervaLoopbackIP, port)
		if tervaValidatePorts(actual, expected, true) == nil {
			t.Fatal("invalid allocated port accepted", port)
		}
	}
	valid := tervaBindings(expected, tervaLoopbackIP, "30000")
	if err := tervaValidatePorts(valid, expected, true); err != nil {
		t.Fatal(err)
	}
	valid[tervaPort("8008/tcp")] = append(valid[tervaPort("8008/tcp")], network.PortBinding{HostIP: netip.MustParseAddr("::"), HostPort: "30000"})
	if tervaValidatePorts(valid, expected, true) == nil {
		t.Fatal("extra IPv6 binding accepted")
	}
	delete(valid, tervaPort("8008/tcp"))
	if tervaValidatePorts(valid, expected, true) == nil {
		t.Fatal("missing port accepted")
	}
	valid[tervaPort("9999/tcp")] = []network.PortBinding{{HostIP: tervaLoopbackIP, HostPort: "30000"}}
	if tervaValidatePorts(valid, expected, true) == nil {
		t.Fatal("wrong port set accepted")
	}
	for _, bad := range []network.PortSet{nil, {tervaPort("8008/tcp"): {}},
		{tervaPort("8008/tcp"): {}, tervaPort("8448/tcp"): {}, tervaPort("0/tcp"): {}},
		{tervaPort("8008/tcp"): {}, tervaPort("8448/tcp"): {}, tervaPort("9000/sctp"): {}}} {
		if _, err := tervaRequestedPorts(bad); err == nil {
			t.Fatal("invalid image ports accepted", bad)
		}
	}
	// Image configurations carry exposed ports as strings; a range or junk fails.
	for _, raw := range []string{"8000-8010/tcp", "bad", "70000/tcp", ""} {
		if _, err := tervaExposedPorts(map[string]struct{}{"8008/tcp": {}, raw: {}}); err == nil {
			t.Fatal("invalid image port string accepted", raw)
		}
	}
	if ports, err := tervaExposedPorts(map[string]struct{}{"8008/tcp": {}, "8448/tcp": {}}); err != nil || len(ports) != 2 {
		t.Fatal("valid image port strings refused", ports, err)
	}
	for _, cfg := range []*config.Complement{{HSPortBindingIP: "0.0.0.0", RunID: "0123456789ab"}, {HSPortBindingIP: "127.0.0.1"}} {
		if _, err := tervaLoopbackPorts(context.Background(), nil, "unused", cfg); err == nil {
			t.Fatal("unsafe config accepted")
		}
	}
}

func TestTervaLoopbackLive(t *testing.T) {
	if os.Getenv("TERVA_LOOPBACK_LIVE") != "1" {
		t.Skip("explicit live probe only")
	}
	cfg := config.NewConfigFromEnvVars("terva_loopback", "")
	builder, err := NewBuilder(cfg)
	if err != nil {
		t.Fatal(err)
	}
	blueprint := b.MustValidate(b.Blueprint{Name: "terva_loopback", Homeservers: []b.Homeserver{{Name: "hs1", Users: []b.User{{Localpart: "@alice"}}}}})
	if os.Getenv("TERVA_LOOPBACK_REJECT") == "1" {
		old := tervaValidateObserved
		defer func() { tervaValidateObserved = old }()
		tervaValidateObserved = func(actual, expected network.PortMap, published bool) error {
			// Corrupt only the inspection data, never the real Docker binding.
			fake := network.PortMap{}
			for p, entries := range actual {
				fake[p] = append([]network.PortBinding(nil), entries...)
			}
			for p := range fake {
				fake[p][0].HostIP = netip.MustParseAddr("::")
				break
			}
			return old(fake, expected, published)
		}
		err := builder.ConstructBlueprintIfNotExist(blueprint)
		if err == nil {
			t.Fatal("injected unsafe response accepted")
		}
		listed, listErr := builder.Docker.ContainerList(context.Background(), client.ContainerListOptions{All: true, Filters: client.Filters{}.Add("label", "complement_run_id="+cfg.RunID)})
		if listErr != nil || len(listed.Items) != 0 {
			t.Fatal("refused container survived", len(listed.Items), listErr)
		}
		if err := os.WriteFile(tervaOutputDir()+"/rejection.json", []byte(`{"rejected":true,"containers_remaining":0}`), 0600); err != nil {
			t.Fatal(err)
		}
		// Deliberate failure proves that the host runner preserves a nonzero test verdict.
		t.Fatal("expected injected unsafe inspection refusal")
	}
	if err := builder.ConstructBlueprintIfNotExist(blueprint); err != nil {
		t.Fatal(err)
	}
	deployer, err := NewDeployer("terva_loopback", cfg)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := deployer.Deploy(context.Background(), blueprint.Name); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(tervaOutputDir()+"/ready", []byte(cfg.RunID), 0600); err != nil {
		t.Fatal(err)
	}
	seconds, _ := strconv.Atoi(os.Getenv("TERVA_LOOPBACK_HOLD_SECONDS"))
	if seconds < 0 || seconds > 300 {
		t.Fatal("invalid hold duration")
	}
	fmt.Printf("loopback fixture ready for %d seconds\n", seconds)
	time.Sleep(time.Duration(seconds) * time.Second)
	// The host runner owns final cleanup, including this intentionally retained deployment.
}
