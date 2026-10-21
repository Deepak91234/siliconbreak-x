/**
 * Webcam access via getUserMedia with an explicit permission flow and a device
 * selector. Device labels are only exposed after permission is granted, so the
 * list is (re)read after the stream starts and on `devicechange`.
 */

import { useCallback, useEffect, useRef, useState } from "react";

export type CameraStatus = "idle" | "requesting" | "live" | "denied" | "unavailable" | "error";

export interface Camera {
  videoRef: React.RefObject<HTMLVideoElement>;
  status: CameraStatus;
  error: string | null;
  devices: MediaDeviceInfo[];
  deviceId: string;
  size: { w: number; h: number } | null;
  start: (deviceId?: string) => Promise<void>;
  stop: () => void;
}

const CONSTRAINTS = { width: { ideal: 640 }, height: { ideal: 480 }, frameRate: { ideal: 30 } };

function describe(e: unknown): { status: CameraStatus; message: string } {
  const name = e && typeof e === "object" && "name" in e ? String((e as { name: unknown }).name) : "";
  const msg = e instanceof Error ? e.message : String(e);
  switch (name) {
    case "NotAllowedError":
    case "SecurityError":
      return { status: "denied", message: "Camera permission was denied. Allow camera access for this page in the browser's site settings (and in macOS System Settings > Privacy & Security > Camera for the browser), then press Enable camera again." };
    case "NotFoundError":
    case "OverconstrainedError":
      return { status: "unavailable", message: "No usable camera was found. Connect a webcam and try again." };
    case "NotReadableError":
    case "AbortError":
      return { status: "error", message: "The camera could not be started; it may be in use by another application." };
    default:
      return { status: "error", message: `Camera error: ${msg}` };
  }
}

export function useCamera(): Camera {
  const videoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const [status, setStatus] = useState<CameraStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [deviceId, setDeviceId] = useState("");
  const [size, setSize] = useState<{ w: number; h: number } | null>(null);

  const refreshDevices = useCallback(async () => {
    try {
      const list = await navigator.mediaDevices.enumerateDevices();
      setDevices(list.filter((d) => d.kind === "videoinput"));
    } catch {
      /* enumerate is best effort */
    }
  }, []);

  const stop = useCallback(() => {
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    if (videoRef.current) videoRef.current.srcObject = null;
    setSize(null);
    setStatus((s) => (s === "live" || s === "requesting" ? "idle" : s));
  }, []);

  const start = useCallback(async (id?: string) => {
    if (!navigator.mediaDevices?.getUserMedia) {
      setStatus("unavailable");
      setError("The camera API is not available. Open the dashboard via http://127.0.0.1:5173 or localhost (a secure context) in a browser with webcam support.");
      return;
    }
    setStatus("requesting");
    setError(null);
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: id ? { ...CONSTRAINTS, deviceId: { exact: id } } : CONSTRAINTS,
        audio: false,
      });
      streamRef.current = stream;
      const track = stream.getVideoTracks()[0];
      track?.addEventListener("ended", () => {
        if (streamRef.current === stream) {
          streamRef.current = null;
          setStatus("error");
          setError("The camera stopped (disconnected or taken by another application).");
        }
      });
      const v = videoRef.current;
      if (v) {
        v.srcObject = stream;
        v.onloadedmetadata = () => setSize({ w: v.videoWidth, h: v.videoHeight });
        await v.play().catch(() => undefined);
        if (v.videoWidth) setSize({ w: v.videoWidth, h: v.videoHeight });
      }
      setDeviceId(track?.getSettings().deviceId ?? id ?? "");
      setStatus("live");
      void refreshDevices();
    } catch (e) {
      const d = describe(e);
      setStatus(d.status);
      setError(d.message);
    }
  }, [refreshDevices]);

  useEffect(() => {
    const md = navigator.mediaDevices;
    if (!md?.addEventListener) return;
    const onChange = () => void refreshDevices();
    md.addEventListener("devicechange", onChange);
    void refreshDevices();
    return () => md.removeEventListener("devicechange", onChange);
  }, [refreshDevices]);

  useEffect(() => () => {
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
  }, []);

  return { videoRef, status, error, devices, deviceId, size, start, stop };
}
