import { useCallback, useState } from "react";

import { ApiError } from "../../api/client";
import { useAuth } from "../../auth/AuthContext";

export const PERMISSION_MESSAGE = "You don't have permission to do this. Only administrators can manage the company.";

/**
 * Error display for admin views, following the server's answer:
 * 401 is handled globally (back to login); 403 means this user is not (or no
 * longer) an admin, so the current user is re-read and the admin section
 * disappears; 404/409/422/429 show the server's message.
 */
export function useAdminErrors() {
  const { refreshUser } = useAuth();
  const [error, setError] = useState<string | null>(null);

  const report = useCallback(
    (caught: unknown) => {
      if (caught instanceof ApiError && caught.status === 403) {
        setError(PERMISSION_MESSAGE);
        void refreshUser();
      } else if (caught instanceof ApiError) {
        setError(caught.message);
      } else {
        setError("Something went wrong. Please try again.");
      }
    },
    [refreshUser],
  );

  return { error, setError, report };
}
