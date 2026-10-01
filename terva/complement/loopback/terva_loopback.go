package docker

import (
	"context"
	"encoding/json"
	"fmt"
	"net/netip"
	"os"
	"regexp"
	"strconv"
	"sync"

	"github.com/matrix-org/complement/config"
	"github.com/moby/moby/api/types/network"
	"github.com/moby/moby/client"
)

var tervaRunID = regexp.MustCompile(`^[a-f0-9]{12}$`)
var tervaEvidenceLock sync.Mutex

// The only host address a binding may use. The moby API carries host IPs as
// netip.Addr, so this is compared by value: the zero Addr, the IPv4-mapped
// ::ffff:127.0.0.1 and any other loopback address all differ from it.
var tervaLoopbackIP = netip.AddrFrom4([4]byte{127, 0, 0, 1})

// Tests can simulate a dishonest inspection response without exposing a real server.
var tervaValidateObserved = tervaValidatePorts

func tervaLoopbackPorts(ctx context.Context, cli *client.Client, imageID string, cfg *config.Complement) (network.PortMap, error) {
	if cfg.HSPortBindingIP != "127.0.0.1" || !tervaRunID.MatchString(cfg.RunID) {
		return nil, fmt.Errorf("terva loopback: require 127.0.0.1 and a 12-hex run ID")
	}
	info, err := cli.ImageInspect(ctx, imageID)
	if err != nil {
		return nil, err
	}
	if info.Config == nil {
		return nil, fmt.Errorf("terva loopback: missing image configuration")
	}
	exposed, err := tervaExposedPorts(info.Config.ExposedPorts)
	if err != nil {
		return nil, err
	}
	return tervaRequestedPorts(exposed)
}

// tervaExposedPorts parses an image's exposed ports, which the image
// configuration carries as strings. A port range or anything unparseable
// fails, rather than being skipped and left to the daemon.
func tervaExposedPorts(raw map[string]struct{}) (network.PortSet, error) {
	exposed := network.PortSet{}
	for s := range raw {
		p, err := network.ParsePort(s)
		if err != nil {
			return nil, fmt.Errorf("terva loopback: invalid exposed port %q", s)
		}
		exposed[p] = struct{}{}
	}
	return exposed, nil
}

func tervaRequestedPorts(exposed network.PortSet) (network.PortMap, error) {
	for _, p := range []network.Port{network.MustParsePort("8008/tcp"), network.MustParsePort("8448/tcp")} {
		if _, ok := exposed[p]; !ok {
			return nil, fmt.Errorf("terva loopback: missing required port %s", p)
		}
	}
	ports := network.PortMap{}
	for p := range exposed {
		if p.Num() == 0 || (p.Proto() != network.TCP && p.Proto() != network.UDP) {
			return nil, fmt.Errorf("terva loopback: invalid exposed port %s", p)
		}
		ports[p] = []network.PortBinding{{HostIP: tervaLoopbackIP}}
	}
	return ports, nil
}

func tervaValidatePorts(actual, expected network.PortMap, published bool) error {
	if len(expected) == 0 || len(actual) != len(expected) {
		return fmt.Errorf("terva loopback: missing or extra ports")
	}
	for p := range expected {
		entries := actual[p]
		if len(entries) != 1 || entries[0].HostIP != tervaLoopbackIP {
			return fmt.Errorf("terva loopback: unsafe binding for %s", p)
		}
		// Docker leaves a requested dynamic port empty until start; podman
		// allocates it at create. Either way a present port must be valid.
		port := entries[0].HostPort
		if published || port != "" {
			n, err := strconv.Atoi(port)
			if err != nil || n < 1 || n > 65535 {
				return fmt.Errorf("terva loopback: invalid host port for %s", p)
			}
		}
	}
	return nil
}

func tervaCheckRequested(ctx context.Context, cli *client.Client, id string, expected network.PortMap) error {
	result, err := cli.ContainerInspect(ctx, id, client.ContainerInspectOptions{})
	if err != nil {
		return err
	}
	item := result.Container
	if item.HostConfig == nil || item.HostConfig.PublishAllPorts || item.HostConfig.NetworkMode.IsHost() {
		return fmt.Errorf("terva loopback: unsafe container configuration")
	}
	return tervaValidatePorts(item.HostConfig.PortBindings, expected, false)
}

func tervaCheckPublished(ctx context.Context, cli *client.Client, id string, expected network.PortMap, cfg *config.Complement, imageID string) error {
	result, err := cli.ContainerInspect(ctx, id, client.ContainerInspectOptions{})
	if err != nil {
		return err
	}
	item := result.Container
	if item.NetworkSettings == nil {
		return fmt.Errorf("terva loopback: no network settings")
	}
	if err := tervaValidateObserved(item.NetworkSettings.Ports, expected, true); err != nil {
		return err
	}
	phase := "blueprint"
	if imageID == cfg.BaseImageURI {
		phase = "base"
	}
	record := map[string]interface{}{"run_id": cfg.RunID, "container_id": id, "phase": phase, "ports": item.NetworkSettings.Ports}
	tervaEvidenceLock.Lock()
	defer tervaEvidenceLock.Unlock()
	f, err := os.OpenFile(tervaOutputDir()+"/bindings.jsonl", os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	defer f.Close()
	return json.NewEncoder(f).Encode(record)
}

// tervaOutputDir is where evidence is written. The tester image mounts
// /terva-output; a run on the host names its own directory.
func tervaOutputDir() string {
	if dir := os.Getenv("TERVA_LOOPBACK_OUTPUT"); dir != "" {
		return dir
	}
	return "/terva-output"
}
