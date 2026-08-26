package hostinfo

import "bytes"

// NVML constants. nvmlSuccess is NVML_SUCCESS; nameBufferSize is
// NVML_DEVICE_NAME_V2_BUFFER_SIZE, the size nvmlDeviceGetName is documented to
// need. Both are plain values in the ABI, not symbols to look up.
const (
	nvmlSuccess    = 0
	nameBufferSize = 96
)

// nvmlName decodes a name buffer NVML filled in: C bytes up to the first NUL.
// A driver that returns an unterminated buffer yields the whole thing rather
// than reading past it.
func nvmlName(buf []byte) string {
	if end := bytes.IndexByte(buf, 0); end >= 0 {
		buf = buf[:end]
	}
	return string(buf)
}
