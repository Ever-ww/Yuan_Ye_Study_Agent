import { Children, isValidElement, useEffect, useId, useState, type ReactElement, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import rehypeKatex from "rehype-katex";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import "katex/dist/katex.min.css";

export function Markdown({ children, className = "markdown", components }: { children: string; className?: string; components?: Components }) {
  return (
    <div className={className}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkMath]}
        rehypePlugins={[rehypeSanitize, rehypeKatex]}
        components={{
          a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer" />,
          code: ({ className: codeClass, children: codeChildren, ...props }) => codeClass === "language-mermaid" ? <Mermaid source={String(codeChildren).replace(/\n$/, "")} /> : <code className={codeClass} {...props}>{codeChildren}</code>,
          pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
          ...components,
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}

function CodeBlock({ children }: { children: ReactNode }) {
  const child = Children.toArray(children)[0];
  if (!isValidElement(child) || child.type !== "code") return <>{children}</>;
  const code = String((child as ReactElement<{ children?: ReactNode }>).props.children ?? "").replace(/\n$/, "");
  const language = String((child as ReactElement<{ className?: string }>).props.className || "").replace(/^language-/, "");
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1_200);
    } catch {
      setCopied(false);
    }
  };
  return <div className="markdown-code-block"><header><span>{language || "code"}</span><button type="button" onClick={() => void copy}>{copied ? "已复制" : "复制"}</button></header><pre>{children}</pre></div>;
}

function Mermaid({ source }: { source: string }) {
  const reactId = useId().replace(/:/g, ""), [svg, setSvg] = useState(""), [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    void import("mermaid").then(async ({ default: mermaid }) => {
      mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: "neutral" });
      const result = await mermaid.render(`yy-mermaid-${reactId}`, source);
      if (active) setSvg(result.svg);
    }).catch((reason) => { if (active) setError(reason instanceof Error ? reason.message : String(reason)); });
    return () => { active = false; };
  }, [reactId, source]);
  if (error) return <pre className="mermaid-error">Mermaid：{error}</pre>;
  if (!svg) return <span className="mermaid-loading">正在绘制图表…</span>;
  return <div className="mermaid-diagram" role="img" aria-label="Mermaid 图表" dangerouslySetInnerHTML={{ __html: svg }} />;
}
