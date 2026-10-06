import type { ReactNode } from "react";

import { Icon } from "./ui/Icon";

/** Login/registration page: a short explanation of the product beside the form. */
export function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <div className="auth">
      <section className="auth__intro" aria-label="About Enterprise Knowledge">
        <div className="brand brand--light">
          <span className="brand__mark" aria-hidden="true"><Icon name="sparkle" size={18} /></span>
          <p className="brand__name">Enterprise Knowledge</p>
        </div>
        <p className="auth__headline">Answers from your company's documents, with the sources to back them up.</p>
        <ul className="auth__points">
          <li>Ask questions in plain language — policies, processes, projects, customers.</li>
          <li>Every answer lists the documents it came from, so you can check it.</li>
          <li>You only ever see documents you are allowed to access.</li>
        </ul>
      </section>
      <main className="auth__form">{children}</main>
    </div>
  );
}
