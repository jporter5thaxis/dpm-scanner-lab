# Validation scope

The working private reference passed 106 development-host tests, including
operator-supplied photograph regressions. The public export omits those private
photographs and their dependent test module; it retains the synthetic/native
decoder, capture, API, tracking and validation tests.

The exact public export passed **94 tests** on the development PC on October 16,
2026. TypeScript checking and the Vite production build passed. The only Python
test warning was a Starlette HTTP-client deprecation. These are software checks,
not a newly measured physical-camera acceptance run.

The public camera producer replaces a private account's fixed settings path
with `DPM_CAMERA_SETTINGS`. Capture and bounded-control logic remain the same.
Hardware operation of that parameterized wrapper still needs a destination
station check; passing software tests is not a camera bring-up test.

## Measured reference checks

| Check | Observed result |
| --- | --- |
| Camera acquisition | Approximately 26-27 fps at 1920x1080 |
| Eight-second HTTP preview test | 191 frames, 23.7 delivered fps |
| Preview network rate in that scene | Approximately 8.29 Mbit/s |
| Two concurrent viewers | 23.5 and 22.9 delivered fps |
| Full-resolution still | 1920x1080, independent of the smaller preview |
| Post-decoder-update live sample | 27 camera fps, 23.9 preview fps, 10.6 processing fps |

The reference operator confirmed good handheld scanning after focusing the
manual lens at approximately six inches. There is no published labeled sample
denominator or fleet-wide accuracy percentage. Camera fps, decoder processing
time and physical acquisition-to-confirmation latency are different metrics.

One matched Jetson synthetic benchmark used 40 scenarios / 240 moving 1080p
frames, actual 10x10-cell Data Matrix symbols, five rotations and multiple
contrast/blur conditions. Both compared versions read all 240 frames with no
wrong values. The optimization reduced mean processing from 73.77 to 9.67 ms
and p95 from 96.30 to 15.96 ms in that version-specific benchmark. Later
real-photo recovery changes added additional acquisition behavior, so these
figures are not a final physical-camera latency guarantee.

## Repeatable acceptance

1. Run the included tests in isolated data and build the frontend.
2. Verify actual fresh frames and a stable native producer on the destination.
3. Measure HTTP preview delivery independently of decoder throughput.
4. Focus each physical lens at the actual scan distance and check glare/motion.
5. Log expected versus decoded IDs, correct/no-decode/incorrect results and
   confirmation latency with sample counts.
6. Verify two-frame agreement, payload rejection, repeated-read feedback,
   duplicate suppression and reintroduction after absence.
7. Check that green tracking disappears when stale and never creates a scan.
8. Check concurrent viewers, reconnect, resource pressure and chosen startup
   behavior under normal production load.

The latest-frame preview should continue even while decoding is busy. There
is a regression test that holds the decoder lock while requiring a new preview.
The 45 ms decoder budget is checked between calls, not a native-call timeout.
