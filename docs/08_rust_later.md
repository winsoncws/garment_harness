# 08 — Rust: when, why, and a first project

## The honest answer first

**Do not write Rust for this project yet.** Not because Rust is hard, but
because nothing here is currently limited by Python's speed:

| What the harness spends time on | Bottleneck | Would Rust help? |
|---------------------------------|------------|------------------|
| waiting for Ollama to generate  | your GPU | no |
| waiting for SQLite to commit    | your disk | no |
| waiting for a camera frame      | the camera | no |
| building prompt strings         | microseconds | no |
| validating a contract           | microseconds | no |
| **the OpenCV frame gate**       | **your CPU** | **yes, eventually** |

One row in that table is a real candidate. That is the whole Rust case
today, and it only becomes real when you have several cameras running.

---

## The rule for when to reach for Rust

Rewrite in Rust when **all four** are true:

1. You profiled it and it is genuinely the bottleneck.
2. It is CPU-bound — not waiting on a GPU, disk, or network.
3. It runs at high frequency (thousands of times a second).
4. Its interface is small and stable — numbers in, numbers out.

The frame gate hits all four *once you have 4+ cameras at 30 fps*. At one
camera, NumPy is comfortably fast enough and you should leave it alone.

**Alternatives to try before Rust**, in order — each is cheaper than
learning a language:

| Option | Effort | Typical gain on the frame gate |
|--------|--------|-------------------------------|
| resize the frame smaller before analysis | 1 line | 4–10× |
| `multiprocessing.Pool` over frames | 10 lines | ~= core count |
| `numba.njit` on the hot function | 2 lines | 5–50× |
| OpenCV's own SIMD paths (`cv2.reduce`) | rewrite the function | 2–5× |
| **Rust** | a weekend | 10–100× |

Try the first four. If they are not enough, then Rust is honest work.

---

## Your first Rust project, already written

`rust/gmt_fast/` contains a real, buildable crate that replaces the two
numeric functions in `t_frame_gate.py`. It is deliberately tiny — ~90 lines
— because your first Rust project should be something you can hold in your
head entirely.

### Step 1 — install (10 minutes)

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source "$HOME/.cargo/env"
rustc --version
pip install maturin
```

`maturin` is the bridge: it compiles Rust into a `.so` that Python can
`import` like any other module. You will not write any C, any `setup.py`,
or any bindings by hand.

### Step 2 — build

```bash
cd rust/gmt_fast
maturin develop --release
```

`--release` is not optional advice. A debug build is roughly 30× slower
than a release build, and people conclude "Rust isn't faster" from that.

### Step 3 — use it

```python
import gmt_fast
score, index = gmt_fast.mad_peak([10.0]*250 + [40.0] + [10.0]*5)
```

### Step 4 — wire it in behind a feature check

```python
try:
    import gmt_fast
    _FAST = True
except ImportError:
    _FAST = False
```

Same shape as every other optional dependency here. Present → use it;
absent → the NumPy path still works.

---

## Reading `src/lib.rs` — the concepts you need, and no more

| Rust | Python equivalent | The one thing to know |
|------|-------------------|-----------------------|
| `let y = 3;` | `y = 3` | **immutable by default** |
| `let mut y = 3;` | `y = 3` | `mut` = "I will change this" |
| `fn f(x: f32) -> f32` | `def f(x: float) -> float:` | types are mandatory |
| `&[f32]` | a read-only view of a list | borrowed — no copy, no ownership |
| `Vec<f32>` | `list[float]` | owns its memory |
| `Option<T>` | `T or None` | the compiler forces you to handle the absent case |
| `Result<T,E>` | returns a value, **or** raises | errors are values, not exceptions |
| `.iter()` | `for x in xs` | no copy |
| `#[pyfunction]` | — | "expose this to Python" |
| `?` | `raise` | propagate the error upward |

**The two ideas that make Rust feel alien, explained once:**

**Ownership.** Every value has exactly one owner. When the owner goes out
of scope, the memory is freed — no garbage collector, no `free()`, no leak.
Passing a value moves ownership; passing `&value` lends it temporarily.

**Borrowing.** You may have many readers (`&T`) *or* one writer (`&mut T`),
never both at once. That rule is checked at compile time, and it is why
Rust has no data races. It is also why the compiler will reject code you
were sure was fine — read the error message; it usually tells you the fix
verbatim.

`rustc`'s error messages are the best of any compiler. When it complains,
it is not being pedantic, and 90% of the time it prints the correction.

---

## What NOT to write in Rust

| Do not port | Why |
|-------------|-----|
| the task/prompt layer | you will edit it daily; Python's iteration speed is the point |
| the store | SQLite is the bottleneck, not the language |
| the router | it spends its life waiting on HTTP |
| the CNN inference | that is CUDA's job; PyTorch is already C++ underneath |
| anything you have not profiled | you will spend a weekend to save 3 ms |

A good end state is **95% Python, 5% Rust** — one small crate holding the
two or three hot numeric loops, called from Python everywhere else.

---

## A realistic timeline

| When | Do |
|------|----|
| now | nothing. Ship the Python. |
| 1 camera | nothing. NumPy is fine. |
| 4+ cameras | profile the gate. Try `multiprocessing`, then `numba`. |
| still too slow | `maturin develop --release` on the crate that is already written. |
| curious before then | build it anyway on a rainy afternoon — it is 90 lines and it is genuinely good practice. |
