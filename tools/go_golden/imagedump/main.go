package main

import (
	"fmt"
	"os"

	"threadfin/src"
)

func main() {
	if err := src.DumpImages(os.Args[1]); err != nil {
		fmt.Println("FAILED", err)
		os.Exit(1)
	}
}
