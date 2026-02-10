// Constant SHM path definitions for all server/client service incoming/outgoing buffers.
//
// This module defines C-compatible string pointers for POSIX shared memory paths
// used for IPC between Rust QUIC components and Python processes.

use libc::c_char;
const SERVER_SHM_SERVICE1_INCOMING: *const c_char =
    c"server-service1-incoming-shm".as_ptr() as *const c_char;
const SERVER_SHM_SERVICE2_INCOMING: *const c_char =
    c"server-service2-incoming-shm".as_ptr() as *const c_char;
const SERVER_SHM_SERVICE3_INCOMING: *const c_char =
    c"server-service3-incoming-shm".as_ptr() as *const c_char;
const SERVER_SHM_SERVICE4_INCOMING: *const c_char =
    c"server-service4-incoming-shm".as_ptr() as *const c_char;
const SERVER_SHM_SERVICE5_INCOMING: *const c_char =
    c"server-service5-incoming-shm".as_ptr() as *const c_char;

const SERVER_SHM_SERVICE1_OUTGOING: *const c_char =
    c"server-service1-outgoing-shm".as_ptr() as *const c_char;
const SERVER_SHM_SERVICE2_OUTGOING: *const c_char =
    c"server-service2-outgoing-shm".as_ptr() as *const c_char;
const SERVER_SHM_SERVICE3_OUTGOING: *const c_char =
    c"server-service3-outgoing-shm".as_ptr() as *const c_char;
const SERVER_SHM_SERVICE4_OUTGOING: *const c_char =
    c"server-service4-outgoing-shm".as_ptr() as *const c_char;
const SERVER_SHM_SERVICE5_OUTGOING: *const c_char =
    c"server-service5-outgoing-shm".as_ptr() as *const c_char;

const CLIENT_SHM_SERVICE1_INCOMING: *const c_char =
    c"client-service1-incoming-shm".as_ptr() as *const c_char;
const CLIENT_SHM_SERVICE2_INCOMING: *const c_char =
    c"client-service2-incoming-shm".as_ptr() as *const c_char;
const CLIENT_SHM_SERVICE3_INCOMING: *const c_char =
    c"client-service3-incoming-shm".as_ptr() as *const c_char;
const CLIENT_SHM_SERVICE4_INCOMING: *const c_char =
    c"client-service4-incoming-shm".as_ptr() as *const c_char;
const CLIENT_SHM_SERVICE5_INCOMING: *const c_char =
    c"client-service5-incoming-shm".as_ptr() as *const c_char;

const CLIENT_SHM_SERVICE1_OUTGOING: *const c_char =
    c"client-service1-outgoing-shm".as_ptr() as *const c_char;
const CLIENT_SHM_SERVICE2_OUTGOING: *const c_char =
    c"client-service2-outgoing-shm".as_ptr() as *const c_char;
const CLIENT_SHM_SERVICE3_OUTGOING: *const c_char =
    c"client-service3-outgoing-shm".as_ptr() as *const c_char;
const CLIENT_SHM_SERVICE4_OUTGOING: *const c_char =
    c"client-service4-outgoing-shm".as_ptr() as *const c_char;
const CLIENT_SHM_SERVICE5_OUTGOING: *const c_char =
    c"client-service5-outgoing-shm".as_ptr() as *const c_char;
