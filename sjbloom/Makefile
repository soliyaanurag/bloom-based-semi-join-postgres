# Makefile for the sjbloom extension.
#
# It uses PGXS, PostgreSQL's build system for extensions. pg_config (found
# on PATH) tells PGXS where the installed PostgreSQL headers and folders are.
#
#   make              compile sjbloom.so
#   make install      copy sjbloom.so, sjbloom.control and the SQL file into
#                     the PostgreSQL install folder
#   make installcheck run the regression test in sql/ against the running
#                     server and compare with expected/

MODULE_big = sjbloom
OBJS = sjbloom.o bloom.o costmodel.o sjb_explain.o
EXTENSION = sjbloom
DATA = sjbloom--1.0.sql
REGRESS = sjbloom_basic
PG_CONFIG = pg_config
PGXS := $(shell $(PG_CONFIG) --pgxs)
include $(PGXS)

# The .c files include sjb_filter.h and costmodel.h, but PGXS does not
# know that. Without this line, changing a header (for example
# SJB_VERSION) would NOT recompile every .o file, and the old value would
# silently stay in sjbloom.so.
$(OBJS): sjb_filter.h costmodel.h
