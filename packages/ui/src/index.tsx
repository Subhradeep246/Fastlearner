export interface AppShellProps {
  readonly title: string;
}

export function AppShell({ title }: AppShellProps) {
  return (
    <main>
      <h1>{title}</h1>
    </main>
  );
}
