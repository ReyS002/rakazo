import { Trans } from "@lingui/react/macro";
import { Button } from "@rakazo/ui-web";
import { Link, useNavigate } from "react-router-dom";
import { WindowChrome } from "./WindowChrome";

export function WelcomePage() {
  const navigate = useNavigate();
  return (
    <div className="flex min-h-full flex-col bg-background" data-rakazo-surface="welcome">
      <div className="app-drag flex gap-2 px-5 py-[18px]">
        <WindowChrome />
      </div>
      <main className="flex flex-1 flex-col items-center justify-center gap-11 px-5 pb-[90px]">
        <div className="flex items-center gap-[18px] sm:gap-[26px]">
          <img
            src="/bull-desk-logo.png"
            alt="Bull Desk"
            className="size-16 overflow-hidden rounded-full sm:size-[88px]"
          />
          <h1 className="text-[56px] leading-none tracking-[-0.03em] text-foreground sm:text-[76px]">
            Bull Desk
          </h1>
        </div>
        <p className="max-w-[600px] text-center text-[27px] leading-[1.4] text-foreground/75">
          <Trans>
            Your team of always-on agents
            <br />
            that you can give real work to.
          </Trans>
        </p>
        <div className="flex flex-wrap items-center justify-center gap-2">
          <Button
            onClick={() => navigate("/sign-up")}
            className="app-no-drag h-auto gap-0 rounded-full px-[34px] py-[15px] text-[19px] font-normal hover:scale-[1.04]"
          >
            <Trans>Sign up</Trans>
            <span aria-hidden="true">&nbsp;&nbsp;→</span>
          </Button>
          <Link
            to="/sign-in"
            className="app-no-drag rounded-full px-[34px] py-[15px] text-[19px] text-foreground/75 transition hover:text-foreground"
          >
            <Trans>Sign in</Trans>
          </Link>
        </div>
      </main>
    </div>
  );
}
