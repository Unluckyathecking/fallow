package modelcache

import (
	"os"
	"path/filepath"
	"strings"

	"github.com/Unluckyathecking/fallow/go-agent/protocol"
)

// On-disk layout (identical to the Python cache):
//
//	cacheDir/<model_id>/<file_name>          verified blob
//	cacheDir/<model_id>/<file_name>.part     in-flight / interrupted download
//	cacheDir/<model_id>/<file_name>.sha256   verification marker
//
// The marker records the sha256 that was verified for the sibling blob; its
// presence-and-match is the cheap "is this model trusted?" signal used on the
// heartbeat hot path (no rehashing of multi-GB files).

func modelDir(cacheDir string, m protocol.ModelManifest) string {
	return filepath.Join(cacheDir, m.ModelID)
}

func blobPath(cacheDir string, m protocol.ModelManifest) string {
	return filepath.Join(modelDir(cacheDir, m), m.FileName)
}

func partPath(cacheDir string, m protocol.ModelManifest) string {
	return filepath.Join(modelDir(cacheDir, m), m.FileName+PartSuffix)
}

func markerPath(cacheDir string, m protocol.ModelManifest) string {
	return filepath.Join(modelDir(cacheDir, m), m.FileName+MarkerSuffix)
}

// readMarker returns the stored sha256 (trimmed of surrounding whitespace), or
// an empty string if the marker is absent or unreadable.
func readMarker(marker string) string {
	data, err := os.ReadFile(marker)
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(data))
}

// writeMarkerAtomic writes the verification marker atomically: it writes a
// sibling temp file and renames it over the marker path.
func writeMarkerAtomic(marker, sha256 string) error {
	tmp := marker + tmpSuffix
	if err := os.WriteFile(tmp, []byte(sha256), 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, marker)
}
