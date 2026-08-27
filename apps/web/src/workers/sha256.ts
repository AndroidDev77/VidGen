/**
 * A dependency-free, incremental SHA-256.
 *
 * The browser's `crypto.subtle.digest` is one-shot: it needs the whole file in
 * memory. A source video can be gigabytes, so the upload path streams the file
 * chunk by chunk through this implementation instead.
 */
const K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

export class Sha256Stream {
  private readonly state = new Uint32Array([
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ]);
  private readonly buffer = new Uint8Array(64);
  private readonly words = new Uint32Array(64);
  private bufferLength = 0;
  private byteLength = 0;

  update(chunk: Uint8Array): void {
    this.byteLength += chunk.length;
    let offset = 0;
    if (this.bufferLength > 0) {
      const needed = Math.min(64 - this.bufferLength, chunk.length);
      this.buffer.set(chunk.subarray(0, needed), this.bufferLength);
      this.bufferLength += needed;
      offset = needed;
      if (this.bufferLength === 64) {
        this.compress(this.buffer, 0);
        this.bufferLength = 0;
      }
    }
    while (offset + 64 <= chunk.length) {
      this.compress(chunk, offset);
      offset += 64;
    }
    if (offset < chunk.length) {
      this.buffer.set(chunk.subarray(offset), 0);
      this.bufferLength = chunk.length - offset;
    }
  }

  digest(): string {
    const bitLength = this.byteLength * 8;
    const tail = new Uint8Array(this.bufferLength < 56 ? 64 : 128);
    tail.set(this.buffer.subarray(0, this.bufferLength));
    tail[this.bufferLength] = 0x80;
    const view = new DataView(tail.buffer);
    view.setUint32(tail.length - 8, Math.floor(bitLength / 0x100000000), false);
    view.setUint32(tail.length - 4, bitLength >>> 0, false);
    for (let offset = 0; offset < tail.length; offset += 64) {
      this.compress(tail, offset);
    }
    let out = "";
    for (const word of this.state) {
      out += word.toString(16).padStart(8, "0");
    }
    return out;
  }

  private compress(block: Uint8Array, offset: number): void {
    const w = this.words;
    for (let i = 0; i < 16; i += 1) {
      const base = offset + i * 4;
      w[i] =
        ((block[base] ?? 0) << 24) |
        ((block[base + 1] ?? 0) << 16) |
        ((block[base + 2] ?? 0) << 8) |
        (block[base + 3] ?? 0);
    }
    for (let i = 16; i < 64; i += 1) {
      const x = w[i - 15] ?? 0;
      const y = w[i - 2] ?? 0;
      const s0 = rotr(x, 7) ^ rotr(x, 18) ^ (x >>> 3);
      const s1 = rotr(y, 17) ^ rotr(y, 19) ^ (y >>> 10);
      w[i] = ((w[i - 16] ?? 0) + s0 + (w[i - 7] ?? 0) + s1) >>> 0;
    }
    let [a, b, c, d, e, f, g, h] = this.state as unknown as number[] as [
      number,
      number,
      number,
      number,
      number,
      number,
      number,
      number,
    ];
    for (let i = 0; i < 64; i += 1) {
      const s1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const temp1 = (h + s1 + ch + (K[i] ?? 0) + (w[i] ?? 0)) >>> 0;
      const s0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (s0 + maj) >>> 0;
      h = g;
      g = f;
      f = e;
      e = (d + temp1) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (temp1 + temp2) >>> 0;
    }
    const next = [a, b, c, d, e, f, g, h];
    for (let i = 0; i < 8; i += 1) {
      this.state[i] = ((this.state[i] ?? 0) + (next[i] ?? 0)) >>> 0;
    }
  }
}

function rotr(value: number, bits: number): number {
  return ((value >>> bits) | (value << (32 - bits))) >>> 0;
}

export interface HashRequest {
  readonly kind: "hash";
  readonly file: Blob;
  readonly chunkSize: number;
}

export type HashResponse =
  | { readonly kind: "progress"; readonly bytesHashed: number; readonly totalBytes: number }
  | { readonly kind: "done"; readonly sha256: string; readonly totalBytes: number }
  | { readonly kind: "error"; readonly message: string };

/**
 * Hash a blob in bounded slices, reporting progress.
 *
 * Only one `chunkSize` slice is resident at a time: the whole file is never
 * read into memory, and its bytes never leave this function.
 */
export async function hashBlob(
  file: Blob,
  chunkSize: number,
  onProgress: (bytesHashed: number, totalBytes: number) => void,
): Promise<string> {
  const stream = new Sha256Stream();
  let offset = 0;
  while (offset < file.size) {
    const end = Math.min(offset + chunkSize, file.size);
    const slice = await file.slice(offset, end).arrayBuffer();
    stream.update(new Uint8Array(slice));
    offset = end;
    onProgress(offset, file.size);
  }
  onProgress(file.size, file.size);
  return stream.digest();
}
