"use strict";
// A QR code for the "Get a DID" page, drawn as SVG: the payment link (debitmydata:ADDRESS?amount=…)
// that a phone wallet scans. Written here so that the page runs no code from elsewhere (its
// Content-Security-Policy allows only its own script). Byte mode, error correction level M
// (15% of the code can be damaged), the smallest version (size) that holds the text, and the
// mask with the lowest penalty score, as in ISO/IEC 18004.
const DmdQr = (() => {
  // Level M, by version (1 to 40): error correction codewords per block, and number of blocks.
  const ECC_PER_BLOCK = [
    -1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26, 26, 28,
    28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28,
  ];
  const BLOCKS = [
    -1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16, 17, 17, 18, 20, 21, 23,
    25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49,
  ];
  const LEVEL_M = 0; // the format bits of level M
  const MASKS = [
    (x, y) => (x + y) % 2 === 0,
    (x, y) => y % 2 === 0,
    (x, y) => x % 3 === 0,
    (x, y) => (x + y) % 3 === 0,
    (x, y) => (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0,
    (x, y) => ((x * y) % 2) + ((x * y) % 3) === 0,
    (x, y) => (((x * y) % 2) + ((x * y) % 3)) % 2 === 0,
    (x, y) => (((x + y) % 2) + ((x * y) % 3)) % 2 === 0,
  ];
  const bit = (value, index) => ((value >>> index) & 1) !== 0;

  // Modules left for data and error correction once the fixed patterns are drawn.
  function rawModules(version) {
    let result = (16 * version + 128) * version + 64;
    if (version >= 2) {
      const align = Math.floor(version / 7) + 2;
      result -= (25 * align - 10) * align - 55;
      if (version >= 7) result -= 36;
    }
    return result;
  }
  const dataCodewords = (version) =>
    Math.floor(rawModules(version) / 8) - ECC_PER_BLOCK[version] * BLOCKS[version];

  // Reed-Solomon error correction over GF(2^8), reducing polynomial 0x11D.
  function multiply(x, y) {
    let z = 0;
    for (let i = 7; i >= 0; i--) {
      z = (z << 1) ^ ((z >>> 7) * 0x11d);
      z ^= ((y >>> i) & 1) * x;
    }
    return z;
  }
  function divisor(degree) {
    const result = new Array(degree).fill(0);
    result[degree - 1] = 1;
    let root = 1;
    for (let i = 0; i < degree; i++) {
      for (let j = 0; j < degree; j++) {
        result[j] = multiply(result[j], root);
        if (j + 1 < degree) result[j] ^= result[j + 1];
      }
      root = multiply(root, 0x02);
    }
    return result;
  }
  function remainder(data, by) {
    const result = by.map(() => 0);
    for (const value of data) {
      const factor = value ^ result.shift();
      result.push(0);
      by.forEach((coefficient, i) => (result[i] ^= multiply(coefficient, factor)));
    }
    return result;
  }

  // The data codewords: byte mode, the length, the bytes, the terminator and the padding.
  function codewords(bytes, version) {
    const bits = [];
    const put = (value, length) => {
      for (let i = length - 1; i >= 0; i--) bits.push((value >>> i) & 1);
    };
    const capacity = dataCodewords(version) * 8;
    put(0b0100, 4);
    put(bytes.length, version <= 9 ? 8 : 16);
    for (const value of bytes) put(value, 8);
    put(0, Math.min(4, capacity - bits.length));
    put(0, (8 - (bits.length % 8)) % 8);
    for (let pad = 0xec; bits.length < capacity; pad ^= 0xec ^ 0x11) put(pad, 8);
    const data = [];
    for (let i = 0; i < bits.length; i += 8) {
      let value = 0;
      for (let j = 0; j < 8; j++) value = (value << 1) | bits[i + j];
      data.push(value);
    }
    // Split into blocks, add each block's error correction, then interleave.
    const blocks = BLOCKS[version];
    const eccLength = ECC_PER_BLOCK[version];
    const raw = Math.floor(rawModules(version) / 8);
    const shortBlocks = blocks - (raw % blocks);
    const shortLength = Math.floor(raw / blocks);
    const by = divisor(eccLength);
    const parts = [];
    for (let i = 0, k = 0; i < blocks; i++) {
      const block = data.slice(k, k + shortLength - eccLength + (i < shortBlocks ? 0 : 1));
      k += block.length;
      const ecc = remainder(block, by);
      if (i < shortBlocks) block.push(0);
      parts.push(block.concat(ecc));
    }
    const result = [];
    for (let i = 0; i < parts[0].length; i++) {
      parts.forEach((part, j) => {
        if (i !== shortLength - eccLength || j >= shortBlocks) result.push(part[i]);
      });
    }
    return result;
  }

  function alignmentPositions(version) {
    if (version === 1) return [];
    const count = Math.floor(version / 7) + 2;
    const step =
      version === 32 ? 26 : Math.ceil((version * 4 + 4) / (count * 2 - 2)) * 2;
    const result = [6];
    for (let position = version * 4 + 10; result.length < count; position -= step) {
      result.splice(1, 0, position);
    }
    return result;
  }

  function build(text, forcedMask) {
    const bytes = Array.from(new TextEncoder().encode(text));
    let version = 1;
    while (4 + (version <= 9 ? 8 : 16) + bytes.length * 8 > dataCodewords(version) * 8) {
      version++;
      if (version > 40) throw new RangeError("The text is too long for a QR code.");
    }
    const size = version * 4 + 17;
    const modules = Array.from({ length: size }, () => new Array(size).fill(false));
    const fixed = Array.from({ length: size }, () => new Array(size).fill(false));
    const set = (x, y, dark) => {
      modules[y][x] = dark;
      fixed[y][x] = true;
    };

    // Timing patterns, then the three finder patterns with their separators.
    for (let i = 0; i < size; i++) {
      set(6, i, i % 2 === 0);
      set(i, 6, i % 2 === 0);
    }
    for (const [cx, cy] of [
      [3, 3],
      [size - 4, 3],
      [3, size - 4],
    ]) {
      for (let dy = -4; dy <= 4; dy++) {
        for (let dx = -4; dx <= 4; dx++) {
          const distance = Math.max(Math.abs(dx), Math.abs(dy));
          const x = cx + dx;
          const y = cy + dy;
          if (x >= 0 && x < size && y >= 0 && y < size) set(x, y, distance !== 2 && distance !== 4);
        }
      }
    }
    // Alignment patterns, except where they would overlap a finder pattern.
    const positions = alignmentPositions(version);
    const last = positions.length - 1;
    positions.forEach((ax, i) => {
      positions.forEach((ay, j) => {
        if ((i === 0 && j === 0) || (i === 0 && j === last) || (i === last && j === 0)) return;
        for (let dy = -2; dy <= 2; dy++) {
          for (let dx = -2; dx <= 2; dx++) {
            set(ax + dx, ay + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
          }
        }
      });
    });

    const drawFormat = (mask) => {
      const data = (LEVEL_M << 3) | mask;
      let rest = data;
      for (let i = 0; i < 10; i++) rest = (rest << 1) ^ ((rest >>> 9) * 0x537);
      const bits = ((data << 10) | rest) ^ 0x5412;
      for (let i = 0; i <= 5; i++) set(8, i, bit(bits, i));
      set(8, 7, bit(bits, 6));
      set(8, 8, bit(bits, 7));
      set(7, 8, bit(bits, 8));
      for (let i = 9; i < 15; i++) set(14 - i, 8, bit(bits, i));
      for (let i = 0; i < 8; i++) set(size - 1 - i, 8, bit(bits, i));
      for (let i = 8; i < 15; i++) set(8, size - 15 + i, bit(bits, i));
      set(8, size - 8, true); // always dark
    };
    drawFormat(0); // reserves the format areas before the data is placed
    if (version >= 7) {
      let rest = version;
      for (let i = 0; i < 12; i++) rest = (rest << 1) ^ ((rest >>> 11) * 0x1f25);
      const bits = (version << 12) | rest;
      for (let i = 0; i < 18; i++) {
        const a = size - 11 + (i % 3);
        const b = Math.floor(i / 3);
        set(a, b, bit(bits, i));
        set(b, a, bit(bits, i));
      }
    }

    // The codewords, in two-module columns zigzagging up and down from the bottom right.
    const all = codewords(bytes, version);
    let index = 0;
    for (let right = size - 1; right >= 1; right -= 2) {
      if (right === 6) right = 5; // skip the vertical timing pattern
      for (let vertical = 0; vertical < size; vertical++) {
        for (let j = 0; j < 2; j++) {
          const x = right - j;
          const y = ((right + 1) & 2) === 0 ? size - 1 - vertical : vertical;
          if (!fixed[y][x] && index < all.length * 8) {
            modules[y][x] = bit(all[index >>> 3], 7 - (index & 7));
            index++;
          }
        }
      }
    }

    const applyMask = (mask) => {
      for (let y = 0; y < size; y++) {
        for (let x = 0; x < size; x++) {
          if (!fixed[y][x] && MASKS[mask](x, y)) modules[y][x] = !modules[y][x];
        }
      }
    };
    let mask = forcedMask;
    if (mask === undefined) {
      let lowest = Infinity;
      for (let candidate = 0; candidate < 8; candidate++) {
        applyMask(candidate);
        drawFormat(candidate);
        const score = penalty(modules);
        if (score < lowest) {
          lowest = score;
          mask = candidate;
        }
        applyMask(candidate); // undo
      }
    }
    applyMask(mask);
    drawFormat(mask);
    return { version, mask, modules };
  }

  // The penalty score of a masked symbol: long runs, 2x2 blocks, finder-like patterns and the
  // balance of dark and light modules.
  function penalty(modules) {
    const size = modules.length;
    let result = 0;
    const addHistory = (length, history) => {
      if (history[0] === 0) length += size; // the light border before the first run
      history.pop();
      history.unshift(length);
    };
    const finderLike = (history) => {
      const n = history[1];
      const core =
        n > 0 && history[2] === n && history[3] === n * 3 && history[4] === n && history[5] === n;
      return (
        (core && history[0] >= n * 4 && history[6] >= n ? 1 : 0) +
        (core && history[6] >= n * 4 && history[0] >= n ? 1 : 0)
      );
    };
    for (let pass = 0; pass < 2; pass++) {
      for (let a = 0; a < size; a++) {
        let color = false;
        let run = 0;
        const history = [0, 0, 0, 0, 0, 0, 0];
        for (let b = 0; b < size; b++) {
          const dark = pass === 0 ? modules[a][b] : modules[b][a];
          if (dark === color) {
            run++;
            if (run === 5) result += 3;
            else if (run > 5) result++;
          } else {
            addHistory(run, history);
            if (!color) result += finderLike(history) * 40;
            color = dark;
            run = 1;
          }
        }
        if (color) {
          addHistory(run, history);
          run = 0;
        }
        addHistory(run + size, history); // the light border after the last run
        result += finderLike(history) * 40;
      }
    }
    let dark = 0;
    for (let y = 0; y < size; y++) {
      for (let x = 0; x < size; x++) {
        if (modules[y][x]) dark++;
        if (
          x < size - 1 &&
          y < size - 1 &&
          modules[y][x] === modules[y][x + 1] &&
          modules[y][x] === modules[y + 1][x] &&
          modules[y][x] === modules[y + 1][x + 1]
        ) {
          result += 3;
        }
      }
    }
    const total = size * size;
    result += (Math.ceil(Math.abs(dark * 20 - total * 10) / total) - 1) * 10;
    return result;
  }

  // The QR code as an SVG element: dark modules on a white square with a 4-module quiet zone,
  // black on white whatever the page's colors, so that every camera reads it.
  function svg(text, label) {
    const { modules } = build(text);
    const border = 4;
    const size = modules.length + border * 2;
    let path = "";
    modules.forEach((row, y) => {
      row.forEach((dark, x) => {
        if (dark) path += `M${x + border},${y + border}h1v1h-1z`;
      });
    });
    const ns = "http://www.w3.org/2000/svg";
    const root = document.createElementNS(ns, "svg");
    root.setAttribute("viewBox", `0 0 ${size} ${size}`);
    root.setAttribute("shape-rendering", "crispEdges");
    root.setAttribute("role", "img");
    root.setAttribute("aria-label", label);
    const background = document.createElementNS(ns, "rect");
    background.setAttribute("width", String(size));
    background.setAttribute("height", String(size));
    background.setAttribute("fill", "#ffffff");
    const marks = document.createElementNS(ns, "path");
    marks.setAttribute("d", path);
    marks.setAttribute("fill", "#000000");
    root.append(background, marks);
    return root;
  }

  return { build, svg };
})();
if (typeof module !== "undefined") module.exports = DmdQr; // for the tests (Node)
