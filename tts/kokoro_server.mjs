// Kokoro text-to-speech helper for the Wili/TooGee assistant.
//
// Loads Kokoro-82M (ONNX, via kokoro-js) once, then serves requests over
// stdin/stdout so the model stays warm between replies:
//
//   stdin  (one JSON object per line): {"id": 1, "text": "Hello!", "voice": "af_heart", "speed": 1.0}
//   stdout: a JSON header line {"id": 1, "rate": 24000, "bytes": N}
//           followed by exactly N bytes of float32 little-endian mono audio.
//           Errors: {"id": 1, "error": "..."} with no audio bytes.
//
// On startup it prints {"ready": true, "voices": [...]} once the model is loaded.
// Logs go to stderr. The model downloads from Hugging Face on first run and is
// cached under node_modules/@huggingface/transformers/.cache afterwards.
import readline from "node:readline";
import { KokoroTTS } from "kokoro-js";

const MODEL = process.env.KOKORO_MODEL || "onnx-community/Kokoro-82M-v1.0-ONNX";
const DTYPE = process.env.KOKORO_DTYPE || "q8"; // fp32 | fp16 | q8 | q4 | q4f16

const log = (...a) => console.error("[kokoro]", ...a);

const t0 = Date.now();
const tts = await KokoroTTS.from_pretrained(MODEL, { dtype: DTYPE, device: "cpu" });
const voices = Object.keys(tts.voices ?? {});
log(`loaded ${MODEL} (${DTYPE}) in ${((Date.now() - t0) / 1000).toFixed(1)}s`);
process.stdout.write(JSON.stringify({ ready: true, voices }) + "\n");

// Requests are handled strictly one at a time so replies never interleave.
let queue = Promise.resolve();
const rl = readline.createInterface({ input: process.stdin });
rl.on("line", (line) => {
  queue = queue.then(() => handle(line));
});
rl.on("close", () => queue.then(() => process.exit(0)));

async function handle(line) {
  let req;
  try {
    req = JSON.parse(line);
  } catch {
    return;
  }
  const id = req.id ?? 0;
  try {
    const started = Date.now();
    const audio = await tts.generate(String(req.text ?? ""), {
      voice: req.voice || "af_heart",
      speed: typeof req.speed === "number" ? req.speed : 1.0,
    });
    const pcm = Buffer.from(audio.audio.buffer, audio.audio.byteOffset, audio.audio.byteLength);
    process.stdout.write(JSON.stringify({ id, rate: audio.sampling_rate, bytes: pcm.length }) + "\n");
    process.stdout.write(pcm);
    log(`#${id}: ${(audio.audio.length / audio.sampling_rate).toFixed(2)}s audio in ${Date.now() - started}ms`);
  } catch (err) {
    process.stdout.write(JSON.stringify({ id, error: String(err?.message ?? err) }) + "\n");
  }
}
