import { useEffect, useState } from "react";

import { AuthProvider, useAuth } from "./auth/AuthContext";
import { AdminPanel } from "./components/admin/AdminPanel";
import { ChatView } from "./components/ChatView";
import { KnowledgeBase } from "./components/KnowledgeBase";
import { AppShell, type Section } from "./components/layout/AppShell";
import { LoginForm } from "./components/LoginForm";

function Screens() {
  const { status, user, refreshUser } = useAuth();
  const [section, setSection] = useState<Section>("chat");
  // Display only: the server decides on every admin request (and answers 403 otherwise).
  const isAdmin = user?.role === "admin";

  // Someone who stops being an admin (e.g. after a 403 re-read /auth/me) leaves the admin section.
  useEffect(() => {
    if (section === "admin" && !isAdmin) setSection("chat");
  }, [section, isAdmin]);

  if (status === "checking") {
    return <p className="centered" role="status">Loading…</p>;
  }
  if (status === "anonymous") {
    // One company: there is no self-service sign-up. Admins add employees (Admin → Users).
    return <LoginForm />;
  }

  function select(next: Section) {
    setSection(next);
    if (next === "admin") void refreshUser(); // re-check the role whenever the admin section is opened
  }

  return (
    <AppShell section={section} onSelect={select} isAdmin={isAdmin}>
      {/* The chat stays mounted while hidden so its conversation survives switching sections. */}
      <main hidden={section !== "chat"} className="page page--chat"><ChatView /></main>
      {section === "documents" && <KnowledgeBase />}
      {section === "admin" && isAdmin && <AdminPanel />}
    </AppShell>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <Screens />
    </AuthProvider>
  );
}
