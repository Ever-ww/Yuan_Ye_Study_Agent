import { useCallback, useEffect, useRef, useState } from "react";
import type { GatewayApi } from "../../api";
import { latestSequence, mergeGatewayEvents, terminalEventTypes } from "../../events";
import type { GatewayEvent } from "../../types";

type ConnectionState = "idle" | "connecting" | "connected" | "reconnecting" | "closed";

export function useRunStream(
  api: GatewayApi,
  runId: string | null,
  onTerminal: (event: GatewayEvent) => void,
) {
  const [events, setEvents] = useState<GatewayEvent[]>([]);
  const [connection, setConnection] = useState<ConnectionState>("idle");
  const terminalCallback = useRef(onTerminal);
  const cursor = useRef(0);
  const retry = useRef(0);
  terminalCallback.current = onTerminal;

  const reset = useCallback(() => {
    cursor.current = 0;
    retry.current = 0;
    setEvents([]);
    setConnection("idle");
  }, []);

  useEffect(() => {
    if (!runId) {
      setConnection("idle");
      return;
    }
    let disposed = false;
    let terminal = false;
    let socket: WebSocket | null = null;
    let timer: number | undefined;

    const connect = () => {
      if (disposed || terminal) return;
      setConnection(retry.current ? "reconnecting" : "connecting");
      socket = api.subscribe(runId, cursor.current, (event) => {
        if (disposed || event.run_id !== runId || event.sequence <= cursor.current) return;
        cursor.current = event.sequence;
        setEvents((current) => mergeGatewayEvents(current, event));
        if (terminalEventTypes.has(event.type)) {
          terminal = true;
          setConnection("closed");
          socket?.close();
          terminalCallback.current(event);
        }
      });
      socket.onopen = () => {
        retry.current = 0;
        setConnection("connected");
      };
      socket.onerror = () => socket?.close();
      socket.onclose = () => {
        if (disposed || terminal) return;
        retry.current += 1;
        setConnection("reconnecting");
        const delay = Math.min(8000, 400 * 2 ** Math.min(retry.current, 5));
        timer = window.setTimeout(connect, delay);
      };
    };

    cursor.current = latestSequence(events);
    connect();
    return () => {
      disposed = true;
      if (timer) window.clearTimeout(timer);
      socket?.close();
    };
    // A Run owns one stream lifecycle. Existing events intentionally do not
    // reconnect the effect; their cursor is tracked by ref.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, runId]);

  return { events, setEvents, connection, reset };
}
