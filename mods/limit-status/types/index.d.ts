// What limit-status keeps in $.state: the band above the prompt while a resumed session is
// compacted (then what Jev saved), or null when there's none.
export type Band = string | null

declare module 'claude-code' {
  interface PluginState {
    'limit-status': { band: Band }
  }
}
