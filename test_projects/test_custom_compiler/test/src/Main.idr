module Main

import CustomCompiler
import System

main : IO ()
main = if greeting == "Custom compiler works!"
          then putStrLn "Test successful!"
          else die "Unexpected greeting"
