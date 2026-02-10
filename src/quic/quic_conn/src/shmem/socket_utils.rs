//! Unsafe Send/Sync pointer wrappers and SHM configuration for shared memory IPC.
//!
//! Defines PtrWrapper/CharWrapper for passing raw pointers across async boundaries,
//! ShmConfig for mapping service IDs to shared memory region names, and the 50MB
//! per-service buffer size constant.

use std::{collections::HashMap, ffi::c_void, sync::Arc};

pub struct PtrWrapper {
    pub shm_ptr: *mut c_void,
}

#[derive(Clone)]
pub struct CharWrapper {
    pub char_ptr: *const i8,
}

unsafe impl Send for PtrWrapper {}
unsafe impl Send for CharWrapper {}
unsafe impl Sync for PtrWrapper {}
unsafe impl Sync for CharWrapper {}

pub const SHM_SIZE: usize = 50000000;

pub struct ShmConfig {
    pub server_incoming_shm_names: HashMap<i32, Arc<CharWrapper>>,
    pub server_outgoing_shm_names: HashMap<i32, Arc<CharWrapper>>,
    pub client_incoming_shm_names: HashMap<i32, Arc<CharWrapper>>,
    pub client_outgoing_shm_names: HashMap<i32, Arc<CharWrapper>>,
}

unsafe impl Send for ShmConfig {}
unsafe impl Sync for ShmConfig {}

pub trait ShmConfigProvider {
    fn provide_shm_config(&self) -> ShmConfig;
}
