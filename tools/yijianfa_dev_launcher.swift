import Foundation
import AppKit

let fileManager = FileManager.default
let launcherExecutable = URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL
let projectRoot = launcherExecutable
    .deletingLastPathComponent()
    .deletingLastPathComponent()
    .deletingLastPathComponent()
    .deletingLastPathComponent()
    .deletingLastPathComponent()
let python = projectRoot.appendingPathComponent(".venv/bin/python")
let liveLauncher = projectRoot.appendingPathComponent("tools/run_source_live.py")
let applicationSupport = fileManager.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support/一键发/logs")
let logPath = applicationSupport.appendingPathComponent("dev-launcher.log")

func stop(_ message: String, _ code: Int32 = 1) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(code)
}

guard fileManager.isExecutableFile(atPath: python.path) else {
    stop("一键发开发版启动失败：未找到当前源码虚拟环境。")
}
guard fileManager.fileExists(atPath: liveLauncher.path) else {
    stop("一键发开发版启动失败：未找到源码联调入口。")
}

do {
    try fileManager.createDirectory(at: applicationSupport, withIntermediateDirectories: true)
    if !fileManager.fileExists(atPath: logPath.path) {
        fileManager.createFile(atPath: logPath.path, contents: nil)
    }
    guard let logHandle = FileHandle(forWritingAtPath: logPath.path) else {
        stop("一键发开发版启动失败：无法写入本机启动日志。")
    }
    try logHandle.seekToEnd()
    logHandle.write(Data("\n开发版原生启动器：使用正式账号数据启动当前源码抖音带货页\n".utf8))

    let process = Process()
    process.currentDirectoryURL = projectRoot
    process.executableURL = python
    process.arguments = ["-u", liveLauncher.path, "--page", "commerce"]
    var environment = ProcessInfo.processInfo.environment
    environment["PYTHONUNBUFFERED"] = "1"
    process.environment = environment
    process.standardOutput = logHandle
    process.standardError = logHandle
    try process.run()
    logHandle.write(Data("开发版子进程已启动：pid=\(process.processIdentifier)\n".utf8))
    process.waitUntilExit()
    logHandle.write(Data("开发版子进程已退出：status=\(process.terminationStatus)\n".utf8))
    logHandle.closeFile()
    if process.terminationStatus != 0 {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "一键发源码联调启动失败"
        alert.informativeText = "请先关闭正式客户端和正在执行的发布任务，再重新打开。详细原因已写入开发版启动日志。"
        alert.addButton(withTitle: "好")
        alert.runModal()
    }
    exit(process.terminationStatus)
} catch {
    stop("一键发开发版启动失败：\(error.localizedDescription)")
}
