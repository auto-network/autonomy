const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO_ROOT = process.env.REPO_ROOT || path.resolve(__dirname, '../../..');
const TERMINAL_MOUNT_JS = path.join(
  REPO_ROOT,
  'tools/dashboard/static/js/lib/terminal-mount.js',
);

class FakeEventTarget {
  constructor() {
    this.listeners = new Map();
    this.dispatched = [];
    this.innerHTML = '';
  }

  addEventListener(type, callback, options) {
    const listeners = this.listeners.get(type) || [];
    listeners.push({ callback, options });
    this.listeners.set(type, listeners);
  }

  removeEventListener(type, callback) {
    const listeners = this.listeners.get(type) || [];
    this.listeners.set(type, listeners.filter((entry) => entry.callback !== callback));
  }

  emit(type, event) {
    for (const entry of this.listeners.get(type) || []) entry.callback(event);
  }

  dispatchEvent(event) {
    this.dispatched.push(event);
    return true;
  }

  listenerCount(type) {
    return (this.listeners.get(type) || []).length;
  }
}

class FakeTerminal {
  constructor() {
    this.rows = 20;
    this.element = new FakeEventTarget();
    this.element.querySelector = (selector) => selector === '.xterm-screen'
      ? { getBoundingClientRect: () => ({ height: 340 }) }
      : null;
    FakeTerminal.instance = this;
  }

  loadAddon() {}
  open() {}
  onData() {}
  onSelectionChange() {}
  attachCustomKeyEventHandler() {}
  write() {}
  dispose() {}
}

class FakeWebSocket {
  constructor() {
    this.readyState = 0;
  }
  close() {}
  send() {}
}
FakeWebSocket.OPEN = 1;

class FakeWheelEvent {
  constructor(type, init) {
    this.type = type;
    Object.assign(this, init);
  }
}

function mount() {
  const container = new FakeEventTarget();
  const sandbox = {
    console,
    document: {
      createElement() { return {}; },
      createEvent() { throw new Error('WheelEvent fallback should not be needed'); },
    },
    location: { protocol: 'https:', host: 'localhost:8080' },
    navigator: {},
    Terminal: FakeTerminal,
    FitAddon: {
      FitAddon: class {
        fit() {}
        proposeDimensions() { return null; }
      },
    },
    WebSocket: FakeWebSocket,
    WheelEvent: FakeWheelEvent,
    window: {},
  };
  sandbox.window.document = sandbox.document;
  vm.createContext(sandbox);
  vm.runInContext(
    fs.readFileSync(TERMINAL_MOUNT_JS, 'utf8'),
    sandbox,
    { filename: 'terminal-mount.js' },
  );
  const mounted = sandbox.window.mountTerminal(container, 'auto-test');
  return { container, mounted, terminal: FakeTerminal.instance };
}

function touchEvent(y, x = 50) {
  return {
    touches: [{ clientY: y, clientX: x }],
    cancelable: true,
    prevented: false,
    stopped: false,
    preventDefault() { this.prevented = true; },
    stopPropagation() { this.stopped = true; },
  };
}

describe('terminal mobile touch scrolling', () => {
  it('translates a finger drag into one wheel event per terminal row', () => {
    const { container, terminal } = mount();
    container.emit('touchstart', touchEvent(200));

    const move = touchEvent(166); // 34px at 17px/row.
    container.emit('touchmove', move);

    assert.equal(move.prevented, true);
    assert.equal(move.stopped, true);
    assert.equal(terminal.element.dispatched.length, 2);
    assert.deepEqual(
      terminal.element.dispatched.map((event) => Math.sign(event.deltaY)),
      [1, 1],
    );
  });

  it('accumulates sub-row motion and preserves drag direction', () => {
    const { container, terminal } = mount();
    container.emit('touchstart', touchEvent(100));
    container.emit('touchmove', touchEvent(109));
    assert.equal(terminal.element.dispatched.length, 0);

    container.emit('touchmove', touchEvent(118));
    assert.equal(terminal.element.dispatched.length, 1);
    assert.equal(Math.sign(terminal.element.dispatched[0].deltaY), -1);
  });

  it('stops handling after touchend and removes listeners on dispose', () => {
    const { container, mounted, terminal } = mount();
    container.emit('touchstart', touchEvent(200));
    container.emit('touchend', { touches: [] });
    container.emit('touchmove', touchEvent(150));
    assert.equal(terminal.element.dispatched.length, 0);

    mounted.dispose();
    for (const type of ['touchstart', 'touchmove', 'touchend', 'touchcancel']) {
      assert.equal(container.listenerCount(type), 0);
    }
  });
});
