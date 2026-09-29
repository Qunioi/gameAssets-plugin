# gameAssets

## Local setup

Start the local server with:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export RUNNINGHUB_API_KEY='your-key'
python server.py
```

The virtual environment avoids macOS/Homebrew's `externally-managed-environment`
restriction on system Python. In a new terminal, activate it again with
`source .venv/bin/activate` before running the server.

The server keeps TLS certificate verification enabled and automatically uses the
standard macOS or Homebrew CA bundle. If the local network uses a trusted
company proxy, set `GAME_ASSETS_CA_BUNDLE` to that proxy's CA bundle before
starting the server.

The settings dialog saves non-secret preferences only. For a local file setup,
add the key to `config.local.json`; that file is ignored by Git and the key is
never accepted or returned by `/api/config`. Existing non-secret preferences in
`config.json` remain compatible; any `api_key` value found there is ignored.
`config.example.json` contains the supported non-secret fields.

By default the server listens on `127.0.0.1:8000`, allows local browser origins
only, and permits file operations under the current user's home directory and
the application directory. Set `GAME_ASSETS_ALLOWED_ROOTS` with
`:`-separated paths when another storage volume is required. Set
`GAME_ASSETS_ALLOWED_ORIGINS` and `RUNNINGHUB_DOWNLOAD_HOSTS` only when the
local deployment needs additional origins or download hosts. RunningHub COS
result hosts matching `rh-...-images-<id>.cos.<region>.myqcloud.com` are
accepted automatically.
When the server is installed inside macOS Apache's
`/Library/WebServer/Documents` (or `/var/www`), that document root is allowed
automatically; otherwise add the exact storage root to
`GAME_ASSETS_ALLOWED_ROOTS` and restart `server.py`.

Any API key that was previously committed should be revoked and replaced.

## Concurrency and RunningHub's queue limit

The Queue's "並行" selector (1-3) controls how many images this app uploads to
RunningHub at the same time, but RunningHub's own account also has a limit on
how many tasks can run at once (commonly fewer than 3, and shared with any
other usage of the same API key). When a submission arrives while that limit
is already reached, RunningHub replies with an `errorCode` such as
`APIKEY_TASK_IS_RUNNING` or `APIKEY_TASK_IS_QUEUED` instead of a task ID.
`server.py` treats this as "someone is currently using a slot" rather than a
failure: it automatically waits and resubmits the same request until a slot
frees up, up to `RUNNINGHUB_BUSY_RETRY_ATTEMPTS` times (default `24`) with
`RUNNINGHUB_BUSY_RETRY_INTERVAL_SECONDS` between attempts (default `5`, so
~120 seconds total). A genuine failure (content moderation, insufficient
balance, invalid parameters, etc.) is never retried and is returned
immediately. Increase either environment variable if your workflows routinely
take longer than 2 minutes and you still want this app to wait instead of
failing.

## Step2 local inpainting selection

Step2 uses workflow `2085215811236577281`. Choose 框選 or 塗鴉 in the prompt
editor, mark the area, and save. Each tool supplies an appropriate default
prompt; custom prompts are preserved. The backend exports a grayscale PNG:
white means edit, black means preserve (soft edges may contain gray).
No selection means whole-image editing with an all-white mask.

`下載測試檔` exports `main.*`, `mask.png`, `prompt.txt`, `payload.json`,
`2___edit_api.json`, and `README.txt`. The image and mask use exactly the same
preparation as the web UI's upload path. Import the bundled API workflow into
RunningHub, upload `main.*` to node **306**, upload `mask.png` to node **318**,
and confirm the prompt at node **25**. Local filenames in JSON do not upload
the files automatically.

Node 324 reads the mask's red channel directly, avoiding LoadImage's inverted
alpha mask output. The Qwen edit encoder still receives the original image and
the aligned black/white mask as two references (image1/image2), so the model
has visual context for the edit region. Node 325 (InpaintModelConditioning)
now feeds that same aligned mask directly into the sampler as a real noise
mask, replacing the previous plain VAEEncode + RepeatLatentBatch latent - this
makes the diffusion process itself only touch the masked region, instead of
generating the whole image and pasting a piece of it back afterward. Node 323
still composites the generated output over the resized original as a final
pixel-level guarantee that anything outside the mask stays untouched. The
workflow retains its existing 1024x1024 padded processing size.

InpaintModelConditioning is a standard ComfyUI node, but its compatibility
with this specific Qwen-Image-Edit-2511 GGUF checkpoint has not been verified
by actually running the workflow - open `2___edit_api.json` in ComfyUI's own
editor first to check for connection/type errors before trusting a batch run.

Before using the web UI with this revision, update and save the cloud workflow
`2085215811236577281` using `api/2___edit_api.json`, then restart `server.py`
and reload the page. Updating the local JSON alone does not update RunningHub.
The old cloud workflow expects alpha inputs and is incompatible with these
new black/white mask files.

## Step4 crop assistant

Select `裁切助手 (step4)` and scan a local folder without calling RunningHub.
Each image is shown in a fixed `169x130` preview with its own `67%` to `300%`
scale control. The source is normalized to a `1344x1024`
canvas first: zooming in crops around the center, while zooming out leaves
black canvas margins. The final downloaded Step4 file is always `1344x1024`.
The crop window uses a fixed `1034x788` area. The folder scan remembers each
image's modification time and refreshes an existing queue item when its file
changes. A centered crop window shows the centered state; after dragging, the
card shows the crop direction controls without a separate top/left/bottom/right
margin panel. Mask color and opacity can be adjusted in the toolbar, which now
lives inside the 處理隊列 card as a single compact row instead of its own
separate card.

Step4 uses one `四邊裁切` action in the same bottom action bar as steps
1-3, with workflow `2085291529685544962`; each image sends its own scale and
current top, bottom, left, and right movement amounts
from the centered position to the local server. For example, moving the crop
window 10 pixels left sends `right=10`. The server applies the scale and crop
locally, then uploads one transparent PNG canvas at exactly `1344x1024`; black
transparent areas are the only regions sent to the AI for background extension.
Step4 does not send a prompt; the fixed prompt is stored directly in
`combined_crop_workflow_api.json` at workflow node `237`, and the fixed negative
prompt is stored at node `234`:
`精細，無縫自然填充黑底部分，保持構圖、特徵、光線、背景、顏色不變`.
The workflow now has one inpainting branch and one final SaveImage node `199`;
the former preview/save branch that produced the `1288x976` framed image was
removed. The server still applies a final `1344x1024` size guard when saving
the RunningHub result.

Downloaded files use the stage prefix followed by the original filename:
`1_upscale_...`, `2_edit_...`, `3_outpaint_...`, and `4_crop_...`. Existing
stage prefixes are removed before the new prefix is applied, and duplicate
names receive an automatic numeric suffix.

`combined_crop_workflow_api.json` is an importable RunningHub API workflow that
expects the cropped image at node `144`. Nodes `229`/`230`/`231`/`232` control
left/right/top/bottom cropping, node `243` performs the crop, and node `184`
extends the canvas before node `185` fixes the result at `1344x1024`. Node
`244` merges the input PNG alpha mask with the extension mask; the merged mask
is expanded and blurred by nodes `187` and `188` before the single inpainting
branch ends at node `199`. Use a transparent RGBA PNG for automatic blank-area
filling. Re-import this JSON into RunningHub after updating it; changing the
local file does not update the already imported cloud workflow automatically.

`combined_crop_workflow_organized.json` is the matching full ComfyUI workflow
with the four directional controls, five canvas groups, and explanatory notes.
