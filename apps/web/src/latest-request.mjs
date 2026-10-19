// Only the newest of overlapping requests may update the screen: an older answer (for the previous
// Knowledge Base, say) can arrive after the newer one.
export function latestRequest() {
  let newest = 0;
  return {
    begin() {
      const id = ++newest;
      return () => id === newest;
    },
  };
}
