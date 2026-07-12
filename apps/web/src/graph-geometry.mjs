export function connectionHandles(sourceNode, targetNode) {
  if (!sourceNode || !targetNode) return {};
  const deltaX = targetNode.position.x - sourceNode.position.x;
  const deltaY = targetNode.position.y - sourceNode.position.y;
  if (Math.abs(deltaX) >= Math.abs(deltaY)) {
    return deltaX >= 0
      ? {sourceHandle: "right", targetHandle: "left"}
      : {sourceHandle: "left", targetHandle: "right"};
  }
  return deltaY >= 0
    ? {sourceHandle: "bottom", targetHandle: "top"}
    : {sourceHandle: "top", targetHandle: "bottom"};
}
