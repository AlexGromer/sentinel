//go:build linux

package store

import (
	"net"
	"syscall"
)

// peerCredListener rejects connections whose peer UID differs from allowedUID via SO_PEERCRED.
// Defense-in-depth for #23 (THREAT_MODEL ❷): the 0600 socket perms already stop other UIDs from
// connecting and the per-run token authenticates same-UID callers; this layer fails closed if the
// socket perms are ever misapplied (e.g. a permissive umask raced the chmod). Linux-only — other
// platforms get the no-op PeerCredListener in peercred_other.go.
type peerCredListener struct {
	*net.UnixListener
	allowedUID uint32
}

func (l *peerCredListener) Accept() (net.Conn, error) {
	for {
		c, err := l.AcceptUnix()
		if err != nil {
			return nil, err
		}
		if l.peerOK(c) {
			return c, nil
		}
		_ = c.Close() // foreign UID -> drop and keep serving
	}
}

func (l *peerCredListener) peerOK(c *net.UnixConn) bool {
	raw, err := c.SyscallConn()
	if err != nil {
		return false
	}
	var ucred *syscall.Ucred
	var sockErr error
	if err := raw.Control(func(fd uintptr) {
		ucred, sockErr = syscall.GetsockoptUcred(int(fd), syscall.SOL_SOCKET, syscall.SO_PEERCRED)
	}); err != nil || sockErr != nil || ucred == nil {
		return false
	}
	return ucred.Uid == l.allowedUID
}

// PeerCredListener wraps a Unix listener so Accept only admits peers whose UID == allowedUID.
// A non-Unix listener is returned unchanged.
func PeerCredListener(l net.Listener, allowedUID uint32) net.Listener {
	ul, ok := l.(*net.UnixListener)
	if !ok {
		return l
	}
	return &peerCredListener{UnixListener: ul, allowedUID: allowedUID}
}
