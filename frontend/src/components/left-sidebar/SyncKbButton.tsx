type SyncKbButtonProps = {
  disabled?: boolean;
};

export function SyncKbButton({ disabled = false }: SyncKbButtonProps) {
  return <button disabled={disabled}>Sync KB</button>;
}