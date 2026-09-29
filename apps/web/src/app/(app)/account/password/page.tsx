import { PageHeader } from "@/components/shell/page-header";
import { Card, CardBody } from "@/components/ui/card";
import { getCurrentUser } from "@/lib/session";

import { PasswordForm } from "./password-form";

/**
 * The only page a temporary password can reach.
 *
 * An account created by an administrator arrives here with
 * `must_change_password` set; the API refuses every other route until it is
 * cleared. The copy says so, because a dashboard full of 403s with no
 * explanation is how a new user concludes the system is broken.
 */
export default async function PasswordPage() {
  const user = await getCurrentUser();

  return (
    <>
      <PageHeader
        title="Password"
        description={
          user?.must_change_password
            ? "This account is using a temporary password chosen by an administrator. Set your own before using the rest of MRIP."
            : "Change the password for this account."
        }
      />
      <div className="px-8 py-6">
        <Card>
          <CardBody>
            <PasswordForm />
          </CardBody>
        </Card>
      </div>
    </>
  );
}
