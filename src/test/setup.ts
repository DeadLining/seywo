import '@testing-library/jest-dom/vitest'

// jsdom does not implement native dialog lifecycle or its browser top layer.
HTMLDialogElement.prototype.showModal = function () { this.open = true }
HTMLDialogElement.prototype.close = function () { this.open = false }
