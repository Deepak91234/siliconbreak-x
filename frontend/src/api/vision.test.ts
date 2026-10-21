import { describe, expect, it } from "vitest";
import { FRAME_HEADER_BYTES, decodeFrame, encodeFrame, parseVisionMessage, visionUrl } from "./vision";

describe("vision wire format", () => {
  it("packs [float64 LE t_ms][JPEG]", () => {
    const jpeg = new Uint8Array([0xff, 0xd8, 0xff, 0xd9]);
    const buf = encodeFrame(1234.5, jpeg);
    expect(buf.byteLength).toBe(FRAME_HEADER_BYTES + jpeg.length);
    expect(new DataView(buf).getFloat64(0, true)).toBe(1234.5);
    const back = decodeFrame(buf);
    expect(back.tMs).toBe(1234.5);
    expect(Array.from(back.jpeg)).toEqual(Array.from(jpeg));
  });
  it("accepts only known server messages", () => {
    expect(parseVisionMessage('{"type":"error","message":"x"}')).toEqual({ type: "error", message: "x" });
    expect(parseVisionMessage('{"type":"other"}')).toBeNull();
    expect(parseVisionMessage("not json")).toBeNull();
    expect(parseVisionMessage(new ArrayBuffer(2))).toBeNull();
  });
  it("builds ws/wss URLs with an encoded session id", () => {
    expect(visionUrl("v 1", { protocol: "http:", host: "127.0.0.1:5173" })).toBe("ws://127.0.0.1:5173/ws/vision/v%201");
    expect(visionUrl("v_1", { protocol: "https:", host: "x" })).toBe("wss://x/ws/vision/v_1");
  });
});
