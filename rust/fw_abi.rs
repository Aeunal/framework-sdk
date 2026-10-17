//! Framework World ABI v1 for Rust nodes.
//!
//! Vendored into each node repo as `src/fw_abi.rs`, the same way a C header
//! would be. Do not edit the copy; re-copy it from framework-sdk when the ABI
//! changes. See ABI.md for the byte-level spec.
#![allow(dead_code)]

pub const ABI_VERSION: u32 = 1;
pub const MAGIC: u32 = 0x3157_5746; // "FWW1"
pub const MAX_BODIES: usize = 64;
pub const MAX_TILES: usize = 4096;

pub mod flags {
    pub const DYNAMIC: u32 = 1;
    pub const GRAVITY: u32 = 2;
    pub const SOLID: u32 = 4;
    pub const PUSHABLE: u32 = 8;
    pub const FRICTION: u32 = 16;
    pub const PLAYER: u32 = 32;
}

pub mod contacts {
    pub const GROUND: u32 = 1;
    pub const CEILING: u32 = 2;
    pub const LEFT: u32 = 4;
    pub const RIGHT: u32 = 8;
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct Header {
    pub magic: u32,
    pub body_count: u32,
    pub tiles_w: u32,
    pub tiles_h: u32,
    pub tile_size: f32,
    pub frame: u32,
    pub _reserved: [u32; 2],
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct Body {
    pub x: f32,
    pub y: f32,
    pub w: f32,
    pub h: f32,
    pub vx: f32,
    pub vy: f32,
    pub mass: f32,
    pub user: f32,
    pub flags: u32,
    pub contacts: u32,
    pub tag: u32,
    pub _reserved: u32,
}

#[repr(C)]
pub struct World {
    pub header: Header,
    pub bodies: [Body; MAX_BODIES],
    pub tiles: [u8; MAX_TILES],
}

const _: () = assert!(core::mem::size_of::<Header>() == 32);
const _: () = assert!(core::mem::size_of::<Body>() == 48);
const _: () = assert!(core::mem::size_of::<World>() == 7200);

impl Body {
    pub const ZERO: Body = Body {
        x: 0.0, y: 0.0, w: 0.0, h: 0.0, vx: 0.0, vy: 0.0, mass: 0.0, user: 0.0,
        flags: 0, contacts: 0, tag: 0, _reserved: 0,
    };

    #[inline]
    pub fn has(&self, flag: u32) -> bool {
        self.flags & flag == flag
    }
}

impl World {
    pub const ZERO: World = World {
        header: Header {
            magic: 0, body_count: 0, tiles_w: 0, tiles_h: 0,
            tile_size: 0.0, frame: 0, _reserved: [0; 2],
        },
        bodies: [Body::ZERO; MAX_BODIES],
        tiles: [0; MAX_TILES],
    };

    #[inline]
    pub fn body_count(&self) -> usize {
        let n = self.header.body_count as usize;
        if n > MAX_BODIES { MAX_BODIES } else { n }
    }

    #[inline]
    pub fn bodies_mut(&mut self) -> &mut [Body] {
        let n = self.body_count();
        &mut self.bodies[..n]
    }

    /// Tile lookup in grid coordinates; anything outside the map is solid.
    #[inline]
    pub fn solid_at(&self, cx: i32, cy: i32) -> bool {
        let (w, h) = (self.header.tiles_w as i32, self.header.tiles_h as i32);
        if cx < 0 || cy < 0 || cx >= w || cy >= h {
            return true;
        }
        let i = (cy * w + cx) as usize;
        i >= MAX_TILES || self.tiles[i] != 0
    }
}

/// `f32::floor` is not in `core`; this is enough for tile indexing.
#[inline]
pub fn floor_i32(v: f32) -> i32 {
    let t = v as i32;
    if (t as f32) > v { t - 1 } else { t }
}

#[cfg(target_arch = "wasm32")]
#[panic_handler]
fn panic(_: &core::panic::PanicInfo) -> ! {
    core::arch::wasm32::unreachable()
}

/// Generates the ABI exports for a node.
///
/// `step` has the signature `fn(&mut World, &[f32], f32)`: world, params, dt.
/// `defaults` is a `const [f32; N]` whose order matches `node.json` params.
#[macro_export]
macro_rules! export_node {
    ($step:path, $defaults:ident) => {
        static mut FW_WORLD: $crate::fw_abi::World = $crate::fw_abi::World::ZERO;
        static mut FW_PARAMS: [f32; $defaults.len()] = $defaults;

        #[no_mangle]
        pub extern "C" fn fw_abi_version() -> u32 {
            $crate::fw_abi::ABI_VERSION
        }

        #[no_mangle]
        pub extern "C" fn fw_world_ptr() -> u32 {
            (&raw const FW_WORLD) as usize as u32
        }

        #[no_mangle]
        pub extern "C" fn fw_world_capacity() -> u32 {
            core::mem::size_of::<$crate::fw_abi::World>() as u32
        }

        #[no_mangle]
        pub extern "C" fn fw_param_count() -> u32 {
            $defaults.len() as u32
        }

        #[no_mangle]
        pub extern "C" fn fw_set_param(index: u32, value: f32) -> u32 {
            let params = unsafe { &mut *(&raw mut FW_PARAMS) };
            match params.get_mut(index as usize) {
                Some(slot) => { *slot = value; 0 }
                None => 1,
            }
        }

        #[no_mangle]
        pub extern "C" fn fw_step(dt: f32) -> u32 {
            let world = unsafe { &mut *(&raw mut FW_WORLD) };
            let params = unsafe { &*(&raw const FW_PARAMS) };
            if world.header.magic != $crate::fw_abi::MAGIC {
                return 1;
            }
            if !(dt > 0.0 && dt < 1.0) {
                return 2;
            }
            $step(world, params, dt);
            0
        }
    };
}
