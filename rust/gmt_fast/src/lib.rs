//! gmt_fast -- the one hot loop in this repo that is worth writing in Rust.
//!
//! WHAT THIS IS
//! ------------
//! A drop-in replacement for `_mad_peak` and the profile scan in
//! `gmt/tasks/t_frame_gate.py`. That function runs on EVERY camera frame --
//! 30 times a second, per camera, forever. It is the only genuinely
//! CPU-bound hot loop in the harness, which is exactly why it is the right
//! (and currently the only) thing to move to Rust.
//!
//! Everything else in the repo waits on a GPU or a disk. Rewriting those in
//! Rust would make them wait in a different language.
//!
//! HOW TO READ THIS FILE IF YOU HAVE NEVER SEEN RUST
//! -------------------------------------------------
//! | Rust                | Python equivalent                            |
//! |---------------------|----------------------------------------------|
//! | `fn f(x: f32) -> f32` | `def f(x: float) -> float:`                |
//! | `let y = 3;`        | `y = 3`  (immutable -- cannot rebind)        |
//! | `let mut y = 3;`    | `y = 3`  (mutable -- `mut` = "I will change it")|
//! | `&[f32]`            | a read-only view of a list of floats         |
//! | `Vec<f32>`          | `list[float]` that owns its memory            |
//! | `PyResult<T>`       | "returns T, or raises a Python exception"    |
//! | `#[pyfunction]`     | "expose this to Python"                      |
//! | `.iter()`           | `for x in xs` without copying                |
//! | `//!` vs `///`      | module doc vs item doc                       |
//!
//! There is no garbage collector and no `None`. If a value might be absent
//! you say so in the type (`Option<T>`), and the compiler forces you to
//! handle it. That is the whole reason Rust is safe.

use pyo3::prelude::*;

/// Median of a slice. Takes `&mut` because sorting happens in place --
/// the caller's copy IS modified, which Rust makes you acknowledge.
fn median(v: &mut Vec<f32>) -> f32 {
    // sort_by because f32 is only PartialOrd (NaN has no defined order),
    // so Rust refuses the plain `.sort()` you would get on integers.
    v.sort_by(|a, b| a.partial_cmp(b).unwrap());
    let n = v.len();
    if n == 0 {
        return 0.0;
    }
    if n % 2 == 1 {
        v[n / 2]
    } else {
        0.5 * (v[n / 2 - 1] + v[n / 2])
    }
}

/// Largest deviation from the median, in MAD units, and where it is.
///
/// Returns `(score, index)`. Median + MAD rather than mean + std because a
/// single large defect drags the mean toward itself and hides the very
/// thing we are looking for.
#[pyfunction]
fn mad_peak(profile: Vec<f32>) -> PyResult<(f32, usize)> {
    if profile.is_empty() {
        return Ok((0.0, 0));
    }
    let mut work = profile.clone();
    let med = median(&mut work);

    let mut devs: Vec<f32> = profile.iter().map(|x| (x - med).abs()).collect();
    let mad = median(&mut devs).max(1e-6);   // never divide by zero
    let scale = 1.4826 * mad;                // MAD -> sigma-equivalent

    let mut best = 0.0f32;
    let mut best_i = 0usize;
    for (i, x) in profile.iter().enumerate() {
        let d = (x - med).abs() / scale;
        if d > best {
            best = d;
            best_i = i;
        }
    }
    Ok((best, best_i))
}

/// Row and column profiles of a greyscale image, in one pass.
///
/// `pixels` is the flattened image (row-major, `width * height` long).
/// Returns `(col_means, row_means)`.
///
/// Doing both in one traversal matters: at 1080p that is 2 million pixels
/// per frame, and touching them twice doubles the cache traffic.
#[pyfunction]
fn profiles(pixels: Vec<f32>, width: usize, height: usize) -> PyResult<(Vec<f32>, Vec<f32>)> {
    if pixels.len() != width * height {
        // This is how you raise a Python exception from Rust.
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "expected {} pixels, got {}",
            width * height,
            pixels.len()
        )));
    }
    let mut col_sum = vec![0.0f32; width];
    let mut row_sum = vec![0.0f32; height];

    for y in 0..height {
        let base = y * width;
        let mut rs = 0.0f32;
        for x in 0..width {
            let v = pixels[base + x];
            rs += v;
            col_sum[x] += v;
        }
        row_sum[y] = rs / width as f32;
    }
    for c in col_sum.iter_mut() {
        *c /= height as f32;
    }
    Ok((col_sum, row_sum))
}

/// The module Python imports. The function name must match `[lib] name`.
#[pymodule]
fn gmt_fast(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(mad_peak, m)?)?;
    m.add_function(wrap_pyfunction!(profiles, m)?)?;
    Ok(())
}
