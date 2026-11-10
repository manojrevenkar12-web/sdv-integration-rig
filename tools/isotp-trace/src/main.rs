//! isotp-trace: read a LINKTYPE_CAN_SOCKETCAN pcap and print every reassembled
//! ISO 15765-2 message as one JSON line: {"t_us":..,"id":..,"data":"hex"}.
//!
//! It is the rig's third, independent decoder: written from the standard, in a
//! different language, sharing no code with the Python stack. It handles what
//! scapy 2.6.1 does not: the CAN FD single-frame escape (SF_DL in byte 1) and the
//! 32-bit first-frame length escape.
//!
//! Usage: isotp-trace <capture.pcap> [can-id ...]   (ids in hex, e.g. 7A1 7A9)

use std::collections::HashMap;
use std::env;
use std::fs;
use std::process::ExitCode;

const LINKTYPE_CAN_SOCKETCAN: u32 = 227;

struct Frame {
    t_us: u64,
    id: u32,
    data: Vec<u8>,
}

#[derive(Default)]
struct Rx {
    expected: usize,
    buf: Vec<u8>,
    next_sn: u8,
    active: bool,
}

fn read_pcap(bytes: &[u8]) -> Result<Vec<Frame>, String> {
    if bytes.len() < 24 {
        return Err("file is shorter than a pcap global header".into());
    }
    let magic = u32::from_le_bytes(bytes[0..4].try_into().unwrap());
    if magic != 0xA1B2_C3D4 {
        return Err(format!("not a little-endian microsecond pcap (magic {magic:#x})"));
    }
    let linktype = u32::from_le_bytes(bytes[20..24].try_into().unwrap());
    if linktype != LINKTYPE_CAN_SOCKETCAN {
        return Err(format!("link type {linktype} is not SocketCAN (227)"));
    }
    let mut frames = Vec::new();
    let mut pos = 24;
    while pos + 16 <= bytes.len() {
        let sec = u32::from_le_bytes(bytes[pos..pos + 4].try_into().unwrap()) as u64;
        let usec = u32::from_le_bytes(bytes[pos + 4..pos + 8].try_into().unwrap()) as u64;
        let incl = u32::from_le_bytes(bytes[pos + 8..pos + 12].try_into().unwrap()) as usize;
        pos += 16;
        if pos + incl > bytes.len() || incl < 8 {
            return Err(format!("truncated record at byte {pos}"));
        }
        let rec = &bytes[pos..pos + incl];
        pos += incl;
        let id = u32::from_be_bytes(rec[0..4].try_into().unwrap()) & 0x1FFF_FFFF;
        let len = rec[4] as usize;
        if 8 + len > rec.len() {
            return Err(format!("frame length {len} exceeds its record"));
        }
        frames.push(Frame { t_us: sec * 1_000_000 + usec, id, data: rec[8..8 + len].to_vec() });
    }
    Ok(frames)
}

/// Feed one frame; return a completed message, if any.
fn feed(rx: &mut Rx, data: &[u8]) -> Result<Option<Vec<u8>>, String> {
    if data.is_empty() {
        return Ok(None);
    }
    match data[0] >> 4 {
        0x0 => {
            let (n, body) = if data[0] & 0x0F != 0 {
                ((data[0] & 0x0F) as usize, &data[1..])
            } else if data.len() > 8 && data.len() >= 2 {
                (data[1] as usize, &data[2..]) // CAN FD escape: SF_DL in byte 1
            } else {
                return Err("single frame with zero length".into());
            };
            if n > body.len() {
                return Err(format!("single frame announces {n} bytes, carries {}", body.len()));
            }
            rx.active = false;
            Ok(Some(body[..n].to_vec()))
        }
        0x1 => {
            let mut n = (((data[0] & 0x0F) as usize) << 8) | data[1] as usize;
            let mut start = 2;
            if n == 0 {
                n = u32::from_be_bytes(data[2..6].try_into().unwrap()) as usize; // 32-bit escape
                start = 6;
            }
            rx.expected = n;
            rx.buf = data[start..].to_vec();
            rx.next_sn = 1;
            rx.active = true;
            Ok(None)
        }
        0x2 => {
            if !rx.active {
                return Ok(None);
            }
            let sn = data[0] & 0x0F;
            if sn != rx.next_sn {
                rx.active = false;
                return Err(format!("consecutive frame {sn} where {} was due", rx.next_sn));
            }
            rx.next_sn = (sn + 1) & 0x0F;
            let need = rx.expected - rx.buf.len();
            let take = need.min(data.len() - 1);
            rx.buf.extend_from_slice(&data[1..1 + take]);
            if rx.buf.len() == rx.expected {
                rx.active = false;
                return Ok(Some(std::mem::take(&mut rx.buf)));
            }
            Ok(None)
        }
        _ => Ok(None), // flow control frames carry no payload
    }
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

fn main() -> ExitCode {
    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {
        eprintln!("usage: isotp-trace <capture.pcap> [can-id-hex ...]");
        return ExitCode::from(2);
    }
    let ids: Vec<u32> = args[2..].iter().filter_map(|a| u32::from_str_radix(a.trim_start_matches("0x"), 16).ok()).collect();
    let bytes = match fs::read(&args[1]) {
        Ok(b) => b,
        Err(e) => {
            eprintln!("cannot read {}: {e}", args[1]);
            return ExitCode::from(1);
        }
    };
    let frames = match read_pcap(&bytes) {
        Ok(f) => f,
        Err(e) => {
            eprintln!("{}: {e}", args[1]);
            return ExitCode::from(1);
        }
    };
    let mut state: HashMap<u32, Rx> = HashMap::new();
    let mut errors = 0;
    for f in &frames {
        if !ids.is_empty() && !ids.contains(&f.id) {
            continue;
        }
        let rx = state.entry(f.id).or_default();
        match feed(rx, &f.data) {
            Ok(Some(msg)) => println!("{{\"t_us\":{},\"id\":{},\"data\":\"{}\"}}", f.t_us, f.id, hex(&msg)),
            Ok(None) => {}
            Err(e) => {
                errors += 1;
                println!("{{\"t_us\":{},\"id\":{},\"error\":\"{}\"}}", f.t_us, f.id, e);
            }
        }
    }
    if errors > 0 { ExitCode::from(3) } else { ExitCode::SUCCESS }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn classic_single_frame() {
        let mut rx = Rx::default();
        assert_eq!(feed(&mut rx, &[0x03, 0x22, 0xF1, 0x89, 0xCC]).unwrap(), Some(vec![0x22, 0xF1, 0x89]));
    }

    #[test]
    fn fd_single_frame_escape() {
        let mut rx = Rx::default();
        let mut frame = vec![0x00, 0x08, 0x62, 0xF1, 0x89, b'3', b'.', b'1', b'.', b'0'];
        frame.resize(12, 0xCC);
        assert_eq!(feed(&mut rx, &frame).unwrap().unwrap().len(), 8);
    }

    #[test]
    fn segmented_message_and_bad_sequence() {
        let mut rx = Rx::default();
        assert_eq!(feed(&mut rx, &[0x10, 0x0A, 1, 2, 3, 4, 5, 6]).unwrap(), None);
        assert_eq!(feed(&mut rx, &[0x21, 7, 8, 9, 10, 0xCC, 0xCC, 0xCC]).unwrap(), Some((1..=10).collect()));
        feed(&mut rx, &[0x10, 0x0A, 1, 2, 3, 4, 5, 6]).unwrap();
        assert!(feed(&mut rx, &[0x22, 7, 8, 9, 10]).is_err());
    }

    #[test]
    fn rejects_other_link_types() {
        let mut header = vec![0xD4, 0xC3, 0xB2, 0xA1, 2, 0, 4, 0];
        header.extend_from_slice(&[0; 12]);
        header.extend_from_slice(&1u32.to_le_bytes());
        assert!(read_pcap(&header).is_err());
    }
}
