import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Markdown } from "./shared/ui/Markdown";

describe("Markdown summary rendering", () => {
  it("renders GFM tables instead of flattening them into paragraphs", () => {
    const html = renderToStaticMarkup(
      <Markdown>
        {"| 指标 | 数值 |\n| --- | ---: |\n| AUROC | 0.91 |"}
      </Markdown>,
    );

    expect(html).toContain("<table>");
    expect(html).toContain("<th>指标</th>");
    expect(html).toContain(">0.91</td>");
  });
});
