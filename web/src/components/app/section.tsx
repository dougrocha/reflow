import { useId, type ReactNode } from "react";

export function Section({
  id, title, description, actions, children, headingLevel = 2, srOnlyTitle = false,
}: {
  id?: string;
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  headingLevel?: 2 | 3;
  srOnlyTitle?: boolean;
}) {
  const headingId = useId();
  const H = headingLevel === 2 ? "h2" : "h3";
  return (
    <section id={id} aria-labelledby={headingId} className="flex flex-col gap-4 sm:gap-6">
      <div className={srOnlyTitle ? "sr-only" : "flex flex-col gap-1 sm:flex-row sm:items-end sm:justify-between sm:gap-4"}>
        <div className="flex flex-col gap-1">
          <H id={headingId} className={headingLevel === 2 ? "type-h2" : "type-h3"}>{title}</H>
          {description ? <p className="type-small text-muted-foreground">{description}</p> : null}
        </div>
        {actions ? <div className="flex flex-wrap gap-2">{actions}</div> : null}
      </div>
      {children}
    </section>
  );
}
