import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { AppShell, StructuredAnswer } from "./index";

describe("AppShell", () => {
  it("renders its accessible heading", () => {
    expect(renderToStaticMarkup(<AppShell title="Study" />)).toContain(
      "<h1>Study</h1>",
    );
  });
});

describe("StructuredAnswer", () => {
  it("renders headings, lists, inline emphasis, and code as semantic content", () => {
    const html = renderToStaticMarkup(<StructuredAnswer text={'# Direct answer\n\n- **First** idea\n- Use `x`\n\n```js\nconst x = 1;\n```'} />);
    expect(html).toContain("<h3><span>Direct answer</span></h3>");
    expect(html).toContain("<ul>");
    expect(html).toContain("<strong>First</strong>");
    expect(html).toContain("<code>x</code>");
    expect(html).toContain("const x = 1;");
  });

  it("renders comparison tables without injecting raw HTML", () => {
    const html = renderToStaticMarkup(<StructuredAnswer text={'Option | Best for\n--- | ---\nA | <script>alert(1)</script>'} />);
    expect(html).toContain("<table>");
    expect(html).toContain("&lt;script&gt;alert(1)&lt;/script&gt;");
    expect(html).not.toContain("<script>");
  });

  it("keeps numbered steps with indented details in one ordered list", () => {
    const html = renderToStaticMarkup(<StructuredAnswer text={'1. **Capture light**\n   - Chlorophyll absorbs energy.\n\n2. **Build glucose**\n   - Carbon dioxide is fixed.\n\n---'} />);
    expect(html.match(/<ol>/g)).toHaveLength(1);
    expect(html.match(/<li>/g)).toHaveLength(2);
    expect(html).toContain("<hr/>");
  });
});
