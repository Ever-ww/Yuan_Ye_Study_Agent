import { createContext, useContext } from "react";
import type { ReactNode } from "react";
import { GatewayApi } from "../../api";

const GatewayApiContext = createContext<GatewayApi | null>(null);

export function GatewayApiProvider({ api, children }: { api: GatewayApi; children: ReactNode }) {
  return <GatewayApiContext.Provider value={api}>{children}</GatewayApiContext.Provider>;
}

export function useGatewayApi(): GatewayApi {
  const api = useContext(GatewayApiContext);
  if (!api) throw new Error("GatewayApiProvider is missing");
  return api;
}
