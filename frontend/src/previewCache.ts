// Session-local preview data only. Source mutation checks never use this cache.
export class PreviewCache {
  private entries = new Map<string, { value: any; expires: number }>();
  get(key: string) {
    const entry = this.entries.get(key);
    if (!entry || entry.expires <= Date.now()) {
      this.entries.delete(key);
      return undefined;
    }
    return structuredClone(entry.value);
  }
  set(key: string, value: any) {
    this.entries.delete(key);
    while (this.entries.size >= 50) this.entries.delete(this.entries.keys().next().value!);
    this.entries.set(key, { value: structuredClone(value), expires: Date.now() + 300_000 });
  }
  clear() { this.entries.clear(); }
}
