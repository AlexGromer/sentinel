//go:build !linux

package store

import "net"

// PeerCredListener is a no-op on non-Linux platforms (SO_PEERCRED is Linux-specific). The per-run
// token (TokenAuthInterceptor) and the 0600 socket perms remain the portable authN layers for #23.
func PeerCredListener(l net.Listener, _ uint32) net.Listener { return l }
