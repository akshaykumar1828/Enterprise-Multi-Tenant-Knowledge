import { useAuth } from "../auth/AuthContext";

export function Header() {
  const { user, logout } = useAuth();
  if (!user) return null;
  return (
    <header className="header">
      <span className="header__title">Enterprise Knowledge</span>
      <div className="header__account">
        <span className="header__org" title={`Organization: ${user.tenant.slug}`}>{user.tenant.name}</span>
        <span className="header__user">{user.display_name ?? user.email}</span>
        <span className="header__role"
              title={user.departments.length ? `Departments: ${user.departments.map((d) => d.name).join(", ")}` : "No departments"}>
          {user.role === "admin" ? "Admin" : "Employee"}
          {user.departments.length > 0 && ` · ${user.departments.map((d) => d.name).join(", ")}`}
        </span>
        <button type="button" className="button button--secondary" onClick={logout}>
          Log out
        </button>
      </div>
    </header>
  );
}
