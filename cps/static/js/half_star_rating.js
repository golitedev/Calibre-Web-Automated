/* First-party half-star rating component for CWA. */
(function (global) {
  "use strict";

  var STEP = 0.5;
  var MAX = 5;

  function normalize(value) {
    var numeric = Number(value);
    if (!isFinite(numeric)) {
      return 0;
    }
    numeric = Math.max(0, Math.min(MAX, numeric));
    return Math.round(numeric * 2) / 2;
  }

  function formatValue(value) {
    return value % 1 === 0 ? String(value) : value.toFixed(1);
  }

  function eventWithDetail(name, detail) {
    var event;
    try {
      event = new CustomEvent(name, { bubbles: true, detail: detail });
    } catch (error) {
      event = document.createEvent("CustomEvent");
      event.initCustomEvent(name, true, false, detail);
    }
    return event;
  }

  function getInput(element) {
    var inputId = element.getAttribute("data-input-id");
    if (inputId) {
      return document.getElementById(inputId);
    }
    var selector = element.getAttribute("data-input-selector");
    if (selector && document.querySelector) {
      return document.querySelector(selector);
    }
    return null;
  }

  function isEditable(element) {
    return element.getAttribute("data-editable") !== "false";
  }

  function render(element, value, preview) {
    var shownValue = preview === null || typeof preview === "undefined" ? value : preview;
    var stars = element.querySelectorAll(".cwa-half-star");
    for (var index = 0; index < stars.length; index += 1) {
      var fill = Math.max(0, Math.min(100, (shownValue - index) * 100));
      stars[index].style.setProperty("--cwa-star-fill", String(fill) + "%");
      stars[index].setAttribute("aria-label", formatValue(Math.min(MAX, index + 1)) + " / 5");
    }
    element.setAttribute("data-value", formatValue(value));
    element.setAttribute("aria-label", formatValue(value) + " / 5");
    element.setAttribute("aria-valuenow", formatValue(value));
    element.setAttribute("aria-valuetext", formatValue(value) + " / 5");
    var input = getInput(element);
    if (input) {
      var inputValue = value === 0 ? "" : formatValue(value);
      if (input.value !== inputValue) {
        input.value = inputValue;
      }
    }
  }

  function getValue(element) {
    if (!element) {
      return 0;
    }
    if (typeof element._cwaHalfStarValue === "number") {
      return element._cwaHalfStarValue;
    }
    return normalize(element.getAttribute("data-value"));
  }

  function setValue(element, value, emit) {
    if (!element) {
      return 0;
    }
    var previous = getValue(element);
    var normalized = normalize(value);
    element._cwaHalfStarValue = normalized;
    render(element, normalized, null);
    if (emit) {
      element.dispatchEvent(eventWithDetail("cwa-half-star-change", {
        value: normalized,
        previousValue: previous
      }));
    }
    return normalized;
  }

  function choose(element, value) {
    var current = getValue(element);
    var normalized = normalize(value);
    if (normalized === current && normalized > 0) {
      normalized = 0;
    }
    setValue(element, normalized, true);
  }

  function starValue(star, event) {
    var rect = star.getBoundingClientRect();
    var point = typeof event.clientX === "number" ? event.clientX : rect.left + rect.width;
    var half = point - rect.left <= rect.width / 2;
    var index = Number(star.getAttribute("data-star-index"));
    return index + (half ? STEP : 1);
  }

  function init(element) {
    if (!element || element._cwaHalfStarInitialized) {
      return element;
    }
    element._cwaHalfStarInitialized = true;
    var editable = isEditable(element);
    if (!element.querySelector(".cwa-half-star")) {
      for (var index = 0; index < MAX; index += 1) {
        var star = document.createElement("span");
        star.className = "cwa-half-star";
        star.setAttribute("data-star-index", String(index));
        star.setAttribute("aria-hidden", "true");
        element.appendChild(star);
      }
    }
    element.setAttribute("role", editable ? "slider" : "img");
    element.setAttribute("aria-valuemin", "0");
    element.setAttribute("aria-valuemax", String(MAX));
    element.setAttribute("aria-valuestep", String(STEP));
    if (editable) {
      element.setAttribute("tabindex", "0");
      element.addEventListener("keydown", function (event) {
        var current = getValue(element);
        var next = current;
        if (event.key === "ArrowLeft") {
          next = Math.max(0, current - STEP);
        } else if (event.key === "ArrowRight") {
          next = Math.min(MAX, current + STEP);
        } else if (event.key === "Delete" || event.key === "Backspace") {
          next = 0;
        } else {
          return;
        }
        event.preventDefault();
        if (next !== current) {
          setValue(element, next, true);
        }
      });
      var stars = element.querySelectorAll(".cwa-half-star");
      for (var starIndex = 0; starIndex < stars.length; starIndex += 1) {
        (function (star) {
          star.addEventListener("mouseenter", function (event) {
            render(element, getValue(element), starValue(star, event));
          });
          star.addEventListener("mousemove", function (event) {
            render(element, getValue(element), starValue(star, event));
          });
          star.addEventListener("mouseleave", function () {
            render(element, getValue(element), null);
          });
          star.addEventListener("click", function (event) {
            choose(element, starValue(star, event));
          });
        })(stars[starIndex]);
      }
      element.addEventListener("mouseleave", function () {
        render(element, getValue(element), null);
      });
    }
    var initial = element.getAttribute("data-value");
    var input = getInput(element);
    if ((initial === null || initial === "") && input) {
      initial = input.value;
    }
    setValue(element, initial || 0, false);
    return element;
  }

  function initAll() {
    var elements = document.querySelectorAll("[data-cwa-half-star-rating]");
    for (var index = 0; index < elements.length; index += 1) {
      init(elements[index]);
    }
  }

  global.CwaHalfStarRating = {
    init: init,
    initAll: initAll,
    set: function (element, value) { return setValue(element, value, false); },
    get: getValue,
    normalize: normalize
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initAll);
  } else {
    initAll();
  }
})(window);
