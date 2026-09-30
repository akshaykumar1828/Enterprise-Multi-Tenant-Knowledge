import { useState } from "react";

import { AuthProvider, useAuth } from "./auth/AuthContext";
import { ChatView } from "./components/ChatView";
import { Header } from "./components/Header";
import { KnowledgeBase } from "./components/KnowledgeBase";
import { LoginForm } from "./components/LoginForm";
import { RegisterForm } from "./components/RegisterForm";

type Tab = "chat" | "knowledge";

function Screens() {
  const { status } = useAuth();
  const [showRegister, setShowRegister] = useState(false);
  const [tab, setTab] = useState<Tab>("chat");

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
      </nav>
      {/* The chat stays mounted while hidden so its history survives switching tabs. */}
      <div hidden={tab !== "chat"} className="tab-panel"><ChatView /></div>
      {tab === "knowledge" && <KnowledgeBase />}
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
