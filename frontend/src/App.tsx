import { useEffect, useState } from "react";

import { AuthProvider, useAuth } from "./auth/AuthContext";
import { AdminPanel } from "./components/admin/AdminPanel";
import { ChatView } from "./components/ChatView";
import { Header } from "./components/Header";
import { KnowledgeBase } from "./components/KnowledgeBase";
import { LoginForm } from "./components/LoginForm";
import { RegisterForm } from "./components/RegisterForm";

type Tab = "chat" | "knowledge" | "admin";

function Screens() {
  const { status, user, refreshUser } = useAuth();
  const [showRegister, setShowRegister] = useState(false);
  const [tab, setTab] = useState<Tab>("chat");
  // Display only: the server decides on every admin request (and answers 403 otherwise).
  const isAdmin = user?.role === "admin";

  // Someone who stops being an admin (e.g. after a 403 re-read /auth/me) leaves the admin section.
  useEffect(() => {
    if (tab === "admin" && !isAdmin) setTab("chat");
  }, [tab, isAdmin]);

  if (status === "checking") {
    return <p className="centered" role="status">Loading…</p>;
  }
  if (status === "anonymous") {
    return (
      <div className="auth-screen">
        {showRegister ? (
          <RegisterForm onShowLogin={() => setShowRegister(false)} />
        ) : (
          <LoginForm onShowRegister={() => setShowRegister(true)} />
        )}
      </div>
    );
  }
  return (
    <div className="app-shell">
      <Header />
      <nav className="tabs" aria-label="Sections">
        <button type="button" className="tab" aria-current={tab === "chat" ? "page" : undefined}
                onClick={() => setTab("chat")}>
          Chat
        </button>
        <button type="button" className="tab" aria-current={tab === "knowledge" ? "page" : undefined}
                onClick={() => setTab("knowledge")}>
          Knowledge base
        </button>
        {isAdmin && (
          <button type="button" className="tab" aria-current={tab === "admin" ? "page" : undefined}
                  onClick={() => {
                    setTab("admin");
                    void refreshUser(); // re-check the role whenever the admin section is opened
                  }}>
            Admin
          </button>
        )}
      </nav>
      {/* The chat stays mounted while hidden so its history survives switching tabs. */}
      <div hidden={tab !== "chat"} className="tab-panel"><ChatView /></div>
      {tab === "knowledge" && <KnowledgeBase />}
      {tab === "admin" && isAdmin && <AdminPanel />}
    </div>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <Screens />
    </AuthProvider>
  );
}
