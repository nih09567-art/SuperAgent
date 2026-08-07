(function initializeResourceRequestGuard(globalScope) {
  const normalizeUserId = (value) => String(value || "").trim();

  const createLatestRequestGuard = (getCurrentUserId) => {
    if (typeof getCurrentUserId !== "function") {
      throw new TypeError("getCurrentUserId must be a function");
    }

    let sequence = 0;

    return {
      begin(userId) {
        sequence += 1;
        return Object.freeze({
          sequence,
          userId: normalizeUserId(userId),
        });
      },

      isCurrent(request) {
        return Boolean(request)
          && request.sequence === sequence
          && request.userId === normalizeUserId(getCurrentUserId());
      },

      invalidate() {
        sequence += 1;
      },
    };
  };

  const api = { createLatestRequestGuard };
  globalScope.ResourceRequestGuard = api;

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
