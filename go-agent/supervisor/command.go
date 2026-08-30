package supervisor

import (
	"path/filepath"
	"strconv"

	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

// llama-server flags appended when a manifest requires GPU offload.
const (
	gpuLayersFlag = "-ngl"
	flashAttnFlag = "--flash-attn"
	mmprojFlag    = "--mmproj"
)

// CommandFactory builds the argv used to launch one replica. It is the
// injection point that turns a manifest, resolved model path, and port into a
// command line with no shell involved. Tests inject a trivial factory that
// spawns a harmless sleeper.
type CommandFactory func(manifest protocol.ModelManifest, modelPath string, port int) []string

// LlamaServerCommand returns the real llama-server CommandFactory bound to cfg.
func LlamaServerCommand(cfg Config) CommandFactory {
	return func(manifest protocol.ModelManifest, modelPath string, port int) []string {
		cmd := []string{
			cfg.LlamaBinary,
			"-m", modelPath,
			"--port", strconv.Itoa(port),
		}
		if manifest.MmprojFileName != nil {
			// The model cache stores the companion beside the main blob.
			mmproj := filepath.Join(filepath.Dir(modelPath), *manifest.MmprojFileName)
			cmd = append(cmd, mmprojFlag, mmproj)
		}
		cmd = append(cmd,
			"--host", cfg.BindHost,
			"--parallel", strconv.Itoa(cfg.Parallel),
			"-c", strconv.Itoa(cfg.ContextSize),
		)
		cmd = append(cmd, manifest.DefaultArgs...)
		if manifest.MinVRAMMB > 0 {
			cmd = append(cmd, gpuLayersFlag, strconv.Itoa(cfg.GPULayers), flashAttnFlag)
		}
		return cmd
	}
}
