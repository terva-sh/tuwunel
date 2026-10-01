// Apply only to the reviewed Complement sources. Different sources fail closed.
package main

import (
	"crypto/sha256"
	"fmt"
	"os"
	"strings"
)

func main() {
	callbacks := map[string]string{}
	for _, patch := range [][3]string{
		{"tests/complemau/harness_test.go", "9c8abeabd85216d055779a051ab315069fffd1835d17f5a435f4882aacdc9532", `Hostname: "0.0.0.0"`},
		{"tests/complemau/ping_errors_test.go", "575722ea3c44f056746f23cfe53e67b85544fc016a8e05490d699d4d4920d5fe", `net.Listen("tcp", fmt.Sprintf("0.0.0.0:%d", port))`},
	} {
		data, err := os.ReadFile(patch[0])
		if err != nil {
			panic(err)
		}
		if fmt.Sprintf("%x", sha256.Sum256(data)) != patch[1] {
			panic("unreviewed Complement callback source: " + patch[0])
		}
		text := string(data)
		if strings.Count(text, patch[2]) != 1 {
			panic("ambiguous callback patch anchor")
		}
		text = strings.Replace(text, patch[2], strings.Replace(patch[2], "0.0.0.0", "127.0.0.1", 1), 1)
		if patch[0] == "tests/complemau/ping_errors_test.go" {
			anchor := "\t\t_ = server.Close()\n\t})\n"
			if strings.Count(text, anchor) != 1 {
				panic("ambiguous callback readiness anchor")
			}
			text = strings.Replace(text, anchor, anchor+"\ttervaWaitForPingCallback(t, port)\n", 1)
		}
		callbacks[patch[0]] = text
	}
	path := "internal/docker/deployer.go"
	data, err := os.ReadFile(path)
	if err != nil {
		panic(err)
	}
	if fmt.Sprintf("%x", sha256.Sum256(data)) != "17788380b7a690597ea5f8cc493d699bd35eb77b9a006be2d5b3d4275d4c1a93" {
		panic("unreviewed Complement deployer")
	}
	text := string(data)
	edits := [][2]string{
		{"\tcapAdd := append([]string{\"NET_ADMIN\"}, cfg.TesteeCapAdd...)\n", "\tcapAdd := append([]string{\"NET_ADMIN\"}, cfg.TesteeCapAdd...)\n\tbindings, err := tervaLoopbackPorts(ctx, docker, imageID, cfg)\n\tif err != nil { return nil, err }\n"},
		{"\t\t\tPublishAllPorts: true,", "\t\t\tPublishAllPorts: false,\n\t\t\tPortBindings: bindings,"},
		{"\tcontainerID := body.ID\n", "\tcontainerID := body.ID\n\ttervaAccepted := false\n\tdefer func() {\n\t\tif !tervaAccepted {\n\t\t\tif _, err := docker.ContainerRemove(context.Background(), containerID, client.ContainerRemoveOptions{Force: true}); err != nil {\n\t\t\t\tlog.Printf(\"terva loopback cleanup failed for %s: %v\", containerID, err)\n\t\t\t}\n\t\t}\n\t}()\n"},
		{"\t_, err = docker.ContainerStart(ctx, containerID, client.ContainerStartOptions{})", "\tif err := tervaCheckRequested(ctx, docker, containerID, bindings); err != nil { return stubDeployment, err }\n\t_, err = docker.ContainerStart(ctx, containerID, client.ContainerStartOptions{})"},
		{"\tbaseURL, fedBaseURL, err := getHostAccessibleHomeserverURLs(ctx, docker, containerID, cfg.HSPortBindingIP)", "\tif err := tervaCheckPublished(ctx, docker, containerID, bindings, cfg, imageID); err != nil { return stubDeployment, err }\n\ttervaAccepted = true\n\tbaseURL, fedBaseURL, err := getHostAccessibleHomeserverURLs(ctx, docker, containerID, cfg.HSPortBindingIP)"},
	}
	for _, e := range edits {
		if strings.Count(text, e[0]) != 1 {
			panic("ambiguous patch anchor")
		}
		text = strings.Replace(text, e[0], e[1], 1)
	}
	// Replace the now-inaccurate upstream publishing comment, not its surrounding code.
	start := strings.Index(text, "\t\t\t// We use `PublishAllPorts`")
	end := strings.Index(text[start:], "\t\t\tPublishAllPorts:") + start
	text = text[:start] + "\t\t\t// Bind every exposed port explicitly; never inherit daemon-wide publishing defaults.\n" + text[end:]
	callbacks[path] = text
	// Validate all three sources before writing any of them.
	for path, text := range callbacks {
		if err := os.WriteFile(path, []byte(text), 0644); err != nil {
			panic(err)
		}
	}
}
