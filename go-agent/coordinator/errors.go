package coordinator

import "fmt"

type AuthError struct {
	Status int
}

func (e *AuthError) Error() string {
	return fmt.Sprintf("coordinator rejected credentials (%d)", e.Status)
}

type ProtocolError struct {
	Message string
}

func (e *ProtocolError) Error() string {
	return e.Message
}

type TransientError struct {
	Message string
	Cause   error
}

func (e *TransientError) Error() string {
	if e.Cause == nil {
		return e.Message
	}
	return fmt.Sprintf("%s: %v", e.Message, e.Cause)
}

func (e *TransientError) Unwrap() error {
	return e.Cause
}
