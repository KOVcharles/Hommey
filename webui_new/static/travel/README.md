# Login travel photographs

Four original images generated with the built-in ImageGen tool for Hommey:

| File | Scene |
| --- | --- |
| `airport.webp` | Airport terminal at dawn |
| `train.webp` | High-speed train window and countryside |
| `arrival.webp` | Arriving at a city hotel with luggage |
| `hotel.webp` | A quiet hotel desk overlooking the city |

All are 1586 × 992, encoded as WebP for delivery (about 552 KiB total).
The photographs have no embedded interface or text. The exact generation
prompts are saved in `prompts.json`; originals remain in the local ImageGen
output directory for this task.

`auth-background.js` holds each scene for 9 seconds and dissolves to the next
over 1.4 seconds. Only two image layers are drawn; the current layer remains
opaque until the next image has decoded and completed its fade. A 3.5% zoom
runs on the compositor. Focus inside the form stops automatic advancement;
the pause control also freezes zoom and any current dissolve. Reduced-motion
and data-saving preferences disable automatic motion; scene buttons still work.

On desktop the background is fixed independently of form/document height.
On mobile the photo scrolls with the header; the form has an opaque paper
background so fields remain readable at every scroll position. Failed image
requests keep the last successfully rendered scene. The first image and full
form remain visible without the background controller.

The first-design rollback point remains at
`reviews/login-before-paper-20260907-202045/`; its original files are unchanged.
