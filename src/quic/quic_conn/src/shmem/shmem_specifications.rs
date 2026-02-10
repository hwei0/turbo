//! Shared memory specification mappings from service IDs to SHM region names.

use std::sync::Arc;

use crate::shmem::socket_utils::{CharWrapper, ShmConfig};

include!("socket_aliases.rs");

pub async fn provide_read_stream_shm_config() -> ShmConfig {
    ShmConfig {
        server_incoming_shm_names: [
            (
                1,
                Arc::new(CharWrapper {
                    char_ptr: SERVER_SHM_SERVICE1_INCOMING,
                }),
            ),
            (
                2,
                Arc::new(CharWrapper {
                    char_ptr: SERVER_SHM_SERVICE2_INCOMING,
                }),
            ),
            (
                3,
                Arc::new(CharWrapper {
                    char_ptr: SERVER_SHM_SERVICE3_INCOMING,
                }),
            ),
            (
                4,
                Arc::new(CharWrapper {
                    char_ptr: SERVER_SHM_SERVICE4_INCOMING,
                }),
            ),
            (
                5,
                Arc::new(CharWrapper {
                    char_ptr: SERVER_SHM_SERVICE5_INCOMING,
                }),
            ),
        ]
        .iter()
        .cloned()
        .collect(),

        server_outgoing_shm_names: [
            (
                1,
                Arc::new(CharWrapper {
                    char_ptr: SERVER_SHM_SERVICE1_OUTGOING,
                }),
            ),
            (
                2,
                Arc::new(CharWrapper {
                    char_ptr: SERVER_SHM_SERVICE2_OUTGOING,
                }),
            ),
            (
                3,
                Arc::new(CharWrapper {
                    char_ptr: SERVER_SHM_SERVICE3_OUTGOING,
                }),
            ),
            (
                4,
                Arc::new(CharWrapper {
                    char_ptr: SERVER_SHM_SERVICE4_OUTGOING,
                }),
            ),
            (
                5,
                Arc::new(CharWrapper {
                    char_ptr: SERVER_SHM_SERVICE5_OUTGOING,
                }),
            ),
        ]
        .iter()
        .cloned()
        .collect(),

        client_incoming_shm_names: [
            (
                1,
                Arc::new(CharWrapper {
                    char_ptr: CLIENT_SHM_SERVICE1_INCOMING,
                }),
            ),
            (
                2,
                Arc::new(CharWrapper {
                    char_ptr: CLIENT_SHM_SERVICE2_INCOMING,
                }),
            ),
            (
                3,
                Arc::new(CharWrapper {
                    char_ptr: CLIENT_SHM_SERVICE3_INCOMING,
                }),
            ),
            (
                4,
                Arc::new(CharWrapper {
                    char_ptr: CLIENT_SHM_SERVICE4_INCOMING,
                }),
            ),
            (
                5,
                Arc::new(CharWrapper {
                    char_ptr: CLIENT_SHM_SERVICE5_INCOMING,
                }),
            ),
        ]
        .iter()
        .cloned()
        .collect(),

        client_outgoing_shm_names: [
            (
                1,
                Arc::new(CharWrapper {
                    char_ptr: CLIENT_SHM_SERVICE1_OUTGOING,
                }),
            ),
            (
                2,
                Arc::new(CharWrapper {
                    char_ptr: CLIENT_SHM_SERVICE2_OUTGOING,
                }),
            ),
            (
                3,
                Arc::new(CharWrapper {
                    char_ptr: CLIENT_SHM_SERVICE3_OUTGOING,
                }),
            ),
            (
                4,
                Arc::new(CharWrapper {
                    char_ptr: CLIENT_SHM_SERVICE4_OUTGOING,
                }),
            ),
            (
                5,
                Arc::new(CharWrapper {
                    char_ptr: CLIENT_SHM_SERVICE5_OUTGOING,
                }),
            ),
        ]
        .iter()
        .cloned()
        .collect(),
    }
}

pub async fn provide_read_zmq_socket_shm_config() -> ShmConfig {
    ShmConfig {
        server_incoming_shm_names: [
            (
                1,
                Arc::new(CharWrapper {
                    char_ptr: SERVER_SHM_SERVICE1_INCOMING,
                }),
            ),
            (
                2,
                Arc::new(CharWrapper {
                    char_ptr: SERVER_SHM_SERVICE2_INCOMING,
                }),
            ),
            (
                3,
                Arc::new(CharWrapper {
                    char_ptr: SERVER_SHM_SERVICE3_INCOMING,
                }),
            ),
            (
                4,
                Arc::new(CharWrapper {
                    char_ptr: SERVER_SHM_SERVICE4_INCOMING,
                }),
            ),
            (
                5,
                Arc::new(CharWrapper {
                    char_ptr: SERVER_SHM_SERVICE5_INCOMING,
                }),
            ),
        ]
        .iter()
        .cloned()
        .collect(),

        server_outgoing_shm_names: [
            (
                1,
                Arc::new(CharWrapper {
                    char_ptr: SERVER_SHM_SERVICE1_OUTGOING,
                }),
            ),
            (
                2,
                Arc::new(CharWrapper {
                    char_ptr: SERVER_SHM_SERVICE2_OUTGOING,
                }),
            ),
            (
                3,
                Arc::new(CharWrapper {
                    char_ptr: SERVER_SHM_SERVICE3_OUTGOING,
                }),
            ),
            (
                4,
                Arc::new(CharWrapper {
                    char_ptr: SERVER_SHM_SERVICE4_OUTGOING,
                }),
            ),
            (
                5,
                Arc::new(CharWrapper {
                    char_ptr: SERVER_SHM_SERVICE5_OUTGOING,
                }),
            ),
        ]
        .iter()
        .cloned()
        .collect(),

        client_incoming_shm_names: [
            (
                1,
                Arc::new(CharWrapper {
                    char_ptr: CLIENT_SHM_SERVICE1_INCOMING,
                }),
            ),
            (
                2,
                Arc::new(CharWrapper {
                    char_ptr: CLIENT_SHM_SERVICE2_INCOMING,
                }),
            ),
            (
                3,
                Arc::new(CharWrapper {
                    char_ptr: CLIENT_SHM_SERVICE3_INCOMING,
                }),
            ),
            (
                4,
                Arc::new(CharWrapper {
                    char_ptr: CLIENT_SHM_SERVICE4_INCOMING,
                }),
            ),
            (
                5,
                Arc::new(CharWrapper {
                    char_ptr: CLIENT_SHM_SERVICE5_INCOMING,
                }),
            ),
        ]
        .iter()
        .cloned()
        .collect(),

        client_outgoing_shm_names: [
            (
                1,
                Arc::new(CharWrapper {
                    char_ptr: CLIENT_SHM_SERVICE1_OUTGOING,
                }),
            ),
            (
                2,
                Arc::new(CharWrapper {
                    char_ptr: CLIENT_SHM_SERVICE2_OUTGOING,
                }),
            ),
            (
                3,
                Arc::new(CharWrapper {
                    char_ptr: CLIENT_SHM_SERVICE3_OUTGOING,
                }),
            ),
            (
                4,
                Arc::new(CharWrapper {
                    char_ptr: CLIENT_SHM_SERVICE4_OUTGOING,
                }),
            ),
            (
                5,
                Arc::new(CharWrapper {
                    char_ptr: CLIENT_SHM_SERVICE5_OUTGOING,
                }),
            ),
        ]
        .iter()
        .cloned()
        .collect(),
    }
}
