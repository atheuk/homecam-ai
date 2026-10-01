import "./styles.css";
export const metadata={title:"HomeCam AI",description:"Local-first camera intelligence",manifest:"/manifest.json"};
export const viewport={themeColor:"#0b0f14"};
export default function Layout({children}:{children:React.ReactNode}){return <html lang="en"><body>{children}</body></html>}
