import { request } from "./api";

export interface AuthSession {
  enabled: boolean;
  authenticated: boolean;
  username: string | null;
}

/** The browser stores no credentials or session token; the server owns its cookie. */
export const authApi = {
  session: (signal?: AbortSignal) => request<AuthSession>("/auth/session", { signal }),
  login: (username: string, password: string) => request<AuthSession>("/auth/login", {
    method: "POST", body: JSON.stringify({ username, password }),
  }),
  logout: () => request<AuthSession>("/auth/logout", { method: "POST" }),
};
