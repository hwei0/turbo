// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
use libc::{c_char, c_void, off_t, size_t};
use libc::{close, ftruncate, memcpy, mmap, shm_open, strncpy};
use libc::{MAP_SHARED, O_CREAT, O_RDWR, PROT_WRITE, S_IRUSR, S_IWUSR};
use s2n_quic::Server;
use s2n_quic::{
    client::Connect,
    provider::{
        congestion_controller::{self, Bbr, Provider},
        event::{self, events::RecoveryMetrics, ConnectionMeta, Subscriber, Timestamp},
    },
    stream::{self, ReceiveStream, SendStream},
    Client,
};
use std::error::Error;
use std::process::Command;
use std::time::Instant;
use std::{
    collections::{HashMap, LinkedList, VecDeque},
    io::Cursor,
    iter::Map,
    net::SocketAddr,
    sync::{atomic::AtomicU32, Arc},
    task::Context,
    thread::sleep,
    time::Duration,
};
use std::{env, ptr, str};
use zeromq::{Socket, SocketRecv, SocketSend};

const STORAGE_ID: *const c_char = b"deez-nuts2\0".as_ptr() as *const c_char;

/// NOTE: this certificate is to be used for demonstration purposes only!
pub static CERT_PEM: &str =
    include_str!(concat!(env!("CARGO_MANIFEST_DIR"), "/../../ssl_cert.pem"));
/// NOTE: this certificate is to be used for demonstration purposes only!
pub static KEY_PEM: &str = include_str!(concat!(env!("CARGO_MANIFEST_DIR"), "/../../ssl_key.pem"));

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    let mut zmq_sock = zeromq::RepSocket::new();
    zmq_sock.bind("ipc:///home/hwei/test-sock").await.unwrap();
    let start = Instant::now();

    let (fd, addr) = unsafe {
        let null = ptr::null_mut();
        // let fd   = shm_open(STORAGE_ID, O_RDWR | O_CREAT, S_IRUSR | S_IWUSR);
        let fd = shm_open(STORAGE_ID, O_RDWR, S_IRUSR);
        let addr = mmap(null, 50000, PROT_WRITE, MAP_SHARED, fd, 0);

        (fd, addr)
    };

    for i in 0..2 {
        // spawn a new task for the connection
        println!(
            "{}",
            String::from_utf8(zmq_sock.recv().await.unwrap().get(0).unwrap().to_vec()).unwrap()
        );

        zmq_sock.send("hello".into()).await.unwrap();

        // // spawn a new task for the connection
        // let size: usize = String::from_utf8(zmq_sock.recv().await.unwrap().get(0).unwrap().to_vec()).unwrap().parse().unwrap();
        // // Consumer...
        // println!("parsing shm msg");

        // let mut data: Vec<i8>  = vec![0; size];
        // let     pdata = data.as_mut_ptr() as *mut c_char;

        // unsafe {
        //     strncpy(pdata, addr as *const c_char, size as size_t);
        //     close(fd);
        // }
        // println!("Producer message (size {}): {:?}", size, data.as_slice());
        // for integy in data.iter() {
        //     println!("{}", integy)
        // }
        // println!("received response of size {} in time {}", size, start.elapsed().as_secs_f64());
    }

    Ok(())
}
