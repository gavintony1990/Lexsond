import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type PropsWithChildren,
} from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, onUnauthorized, setCsrfToken } from "./api";
import type { AuthSessionState } from "./types";

type AuthPhase = "loading" | "authenticated" | "anonymous" | "error";

interface AuthContextValue {
  phase: AuthPhase;
  session: AuthSessionState | null;
  error: unknown;
  login(email: string, password: string, returnTo: string | null): Promise<string>;
  register(email: string, password: string, displayName: string): Promise<void>;
  verifyEmail(token: string): Promise<void>;
  forgotPassword(email: string): Promise<void>;
  resetPassword(token: string, newPassword: string): Promise<void>;
  changePassword(currentPassword: string, newPassword: string): Promise<void>;
  logout(): Promise<void>;
  retry(): void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: PropsWithChildren) {
  const queryClient = useQueryClient();
  const [session, setSession] = useState<AuthSessionState | null>(null);
  const [forcedAnonymous, setForcedAnonymous] = useState(false);
  const sessionQuery = useQuery({
    queryKey: ["auth", "session"],
    queryFn: api.authSession,
    retry: false,
    staleTime: 0,
  });

  const becomeAnonymous = useCallback(() => {
    setCsrfToken(null);
    setSession(null);
    setForcedAnonymous(true);
    queryClient.removeQueries({ queryKey: ["bootstrap"] });
  }, [queryClient]);

  useEffect(() => {
    onUnauthorized(becomeAnonymous);
    return () => onUnauthorized(null);
  }, [becomeAnonymous]);

  useEffect(() => {
    if (!sessionQuery.data) return;
    setCsrfToken(sessionQuery.data.csrf_token);
    setSession(sessionQuery.data);
    setForcedAnonymous(false);
  }, [sessionQuery.data]);

  const beginPublicMutation = useCallback(async () => {
    const issued = await api.authCsrf();
    setCsrfToken(issued.csrf_token);
  }, []);

  const login = useCallback(async (email: string, password: string, returnTo: string | null) => {
    await beginPublicMutation();
    try {
      const result = await api.login(email, password, returnTo);
      const next: AuthSessionState = {
        user: result.user,
        csrf_token: result.csrf_token,
        auth_mode: result.auth_mode,
      };
      setCsrfToken(result.csrf_token);
      setSession(next);
      setForcedAnonymous(false);
      queryClient.setQueryData(["auth", "session"], next);
      return result.return_to;
    } finally {
      password = "";
    }
  }, [beginPublicMutation, queryClient]);

  const register = useCallback(async (email: string, password: string, displayName: string) => {
    await beginPublicMutation();
    try {
      await api.register(email, password, displayName);
    } finally {
      password = "";
    }
  }, [beginPublicMutation]);

  const verifyEmail = useCallback(async (token: string) => {
    await beginPublicMutation();
    try {
      await api.verifyEmail(token);
    } finally {
      token = "";
    }
  }, [beginPublicMutation]);

  const forgotPassword = useCallback(async (email: string) => {
    await beginPublicMutation();
    await api.forgotPassword(email);
  }, [beginPublicMutation]);

  const resetPassword = useCallback(async (token: string, newPassword: string) => {
    await beginPublicMutation();
    try {
      await api.resetPassword(token, newPassword);
    } finally {
      token = "";
      newPassword = "";
    }
  }, [beginPublicMutation]);

  const changePassword = useCallback(async (currentPassword: string, newPassword: string) => {
    try {
      await api.changePassword(currentPassword, newPassword);
    } finally {
      currentPassword = "";
      newPassword = "";
    }
    becomeAnonymous();
    queryClient.clear();
  }, [becomeAnonymous, queryClient]);

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } finally {
      becomeAnonymous();
      queryClient.clear();
    }
  }, [becomeAnonymous, queryClient]);

  const unauthorized = sessionQuery.error instanceof ApiError && sessionQuery.error.status === 401;
  const phase: AuthPhase = session
    ? "authenticated"
    : forcedAnonymous
      ? "anonymous"
    : sessionQuery.isPending
      ? "loading"
      : unauthorized
        ? "anonymous"
        : "error";
  const value = useMemo<AuthContextValue>(() => ({
    phase,
    session,
    error: sessionQuery.error,
    login,
    register,
    verifyEmail,
    forgotPassword,
    resetPassword,
    changePassword,
    logout,
    retry: () => void sessionQuery.refetch(),
  }), [phase, session, sessionQuery.error, sessionQuery.refetch, login, register, verifyEmail, forgotPassword, resetPassword, changePassword, logout]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth must be used inside AuthProvider");
  return value;
}

export function useOptionalAuth(): AuthContextValue | null {
  return useContext(AuthContext);
}
